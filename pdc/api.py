"""FastAPI 服务层（生产向）。

四个变化，都是从"能演示"到"能给别人用"：

    1. 鉴权       Bearer token；没配 token 时只允许本机访问
    2. 限流       按 IP 的令牌桶，超出 429 + Retry-After
    3. 异步任务   POST /runs 立即返回 202，后台跑，GET 轮询
    4. 持久化     SQLite 存任务，重启不丢

端点：
    GET  /                    前端页面
    GET  /health              健康检查
    GET  /pipelines           可选流水线列表
    POST /runs                提交任务（202）
    GET  /runs/{id}           查询状态
    GET  /runs/{id}/trace     每一步的轨迹
    GET  /runs/{id}/stream    SSE 实时推送
    POST /runs/{id}/cancel    取消
    GET  /files/{name}        下载产物
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pdc.agent import run_with_self_check
from pdc.jobs import JobStore
from pdc.llm import OpenAICompatibleLLM, RuleBasedLLM
from pdc.paths import describe, out_dir, repo_dir
from pdc.security import RateLimiter, require_token, token_budget
from pdc.tools import Toolbox
from pdc.trace import Tracer

app = FastAPI(title="Pipeline Design Copilot", version="1.0.0")

OUT = out_dir()
STORE = JobStore(OUT / "jobs.db")
STORE.mark_orphans()  # 上次进程留下的 queued/running 全部标失败

app.mount("/files", StaticFiles(directory=str(OUT)), name="files")

MAX_CONCURRENCY = int(os.getenv("PDC_MAX_CONCURRENCY", "4"))
_active = 0


def _admit() -> bool:
    """同步准入：检查 + 占位在同一步完成，避免两个请求同时通过。"""
    global _active
    if _active >= MAX_CONCURRENCY:
        return False
    _active += 1
    return True


def _release() -> None:
    global _active
    _active = max(0, _active - 1)
_limiter = RateLimiter(per_min=int(os.getenv("PDC_RATE_PER_MIN", "6")), burst=2)
_CANCELS: set[str] = set()


async def _rate_limit(request: Request) -> None:
    """注意：不能在模块导入时把 limiter 绑进闭包，
    否则测试里替换 api._limiter 不生效，限流会跨用例累积。"""
    client = request.client.host if request.client else "unknown"
    ok, wait = _limiter.check(client)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请 {wait}s 后重试",
            headers={"Retry-After": str(int(wait) + 1)},
        )
_LIVE: dict[str, list[dict]] = {}   # run_id -> 实时步骤（仅内存，用于 SSE）

UI_DIR = Path(__file__).parent / "ui"


class RunRequest(BaseModel):
    pipeline: str = Field(min_length=1)
    mode: str = Field(default="offline", pattern="^(offline|auto)$")
    model: str = ""
    max_steps: int = 15


# ---------------------------------------------------------------- 基础
@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "max_concurrency": MAX_CONCURRENCY,
        "in_flight": _active,
        "rate_limit_per_min": _limiter.per_min,
        "token_budget": token_budget(),
        "auth_required": bool(os.getenv("PDC_API_TOKEN", "").strip()),
        "paths": describe(),
    }


@app.get("/")
def index():
    page = UI_DIR / "index.html"
    if not page.exists():
        return {"message": "UI 未安装，访问 /docs 使用 Swagger"}
    return FileResponse(page)


@app.get("/pipelines")
def pipelines(
    _auth: None = Depends(require_token),
    _rate: None = Depends(_rate_limit),
) -> dict:
    base = repo_dir()
    files = sorted(p.name for p in base.glob("*.yaml"))
    return {"directory": str(base), "pipelines": files}


# ---------------------------------------------------------------- 任务
@app.post(
    "/runs",
    status_code=202,
    responses={401: {}, 429: {}},
)
async def create_run(
    req: RunRequest,
    _auth: None = Depends(require_token),
    _rate: None = Depends(_rate_limit),
) -> dict:
    if not _admit():
        raise HTTPException(
            status_code=429,
            detail=f"并发已满（{MAX_CONCURRENCY}）",
            headers={"Retry-After": "2"},
        )

    run_id = uuid.uuid4().hex[:12]
    try:
        STORE.create(run_id, req.pipeline, req.mode)
    except Exception:
        _release()
        raise
    _LIVE[run_id] = []

    asyncio.create_task(asyncio.to_thread(_run_and_release, run_id, req))
    return {"run_id": run_id, "status": "queued", "poll": f"/runs/{run_id}"}


def _run_and_release(run_id: str, req: RunRequest) -> None:
    try:
        _execute_and_store(run_id, req)
    finally:
        _release()


@app.get("/runs/{run_id}")
def get_run(run_id: str, _: None = Depends(require_token)) -> dict:
    record = STORE.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return record.to_dict()


@app.get("/runs/{run_id}/trace")
def get_trace(run_id: str, _: None = Depends(require_token)) -> dict:
    if STORE.get(run_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    path = OUT / f"{run_id}.trace.jsonl"
    steps = []
    if path.exists():
        steps = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"run_id": run_id, "steps": steps}


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, _: None = Depends(require_token)) -> dict:
    record = STORE.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if record.status not in ("queued", "running"):
        return {"run_id": run_id, "status": record.status, "note": "任务已结束"}
    _CANCELS.add(run_id)
    return {"run_id": run_id, "status": "cancelling", "note": "当前步骤结束后停止"}


@app.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> StreamingResponse:
    """SSE：先回放已产生的步骤，再实时推送新的。"""
    if STORE.get(run_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def gen():
        sent = 0
        idle = 0
        while True:
            steps = _LIVE.get(run_id, [])
            while sent < len(steps):
                yield "data: " + json.dumps(steps[sent], ensure_ascii=False) + "\n\n"
                sent += 1

            record = STORE.get(run_id)
            if (
                record
                and record.status in ("succeeded", "failed", "cancelled")
                and sent >= len(steps)
            ):
                yield "data: " + json.dumps(
                    {"kind": "done", "status": record.status, "files": record.files},
                    ensure_ascii=False,
                ) + "\n\n"
                return

            if await request.is_disconnected():
                return

            await asyncio.sleep(0.4)
            idle += 1
            if idle > 900:  # 6 分钟无进展，主动断开
                return

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------- 执行
def _execute_and_store(run_id: str, req: RunRequest) -> None:
    trace_path = OUT / f"{run_id}.trace.jsonl"
    tracer = Tracer(path=trace_path)

    def on_step(step) -> None:
        _LIVE.setdefault(run_id, []).append({
            "index": step.index,
            "kind": step.kind,
            "name": step.name,
            "latency_ms": step.latency_ms,
            "tokens_in": step.tokens_in,
            "tokens_out": step.tokens_out,
            "error": step.error,
        })

    tracer.on_step = on_step
    STORE.update(run_id, status="running")

    prompt = (
        f"为 pipeline `{req.pipeline}` 生成设计文档。\n"
        "1. 先用工具获取事实\n2. 列数/差异必须调用 diff_schema\n"
        "3. 生成后自检，有问题就修\n4. 汇报文件路径和 high 级问题数量"
    )

    try:
        toolbox = Toolbox(out_dir=OUT, tracer=tracer)
        llm = (
            OpenAICompatibleLLM(req.model)
            if req.mode == "auto"
            else RuleBasedLLM()
        )
        result = run_with_self_check(
            llm,
            toolbox,
            prompt,
            max_steps=req.max_steps,
            user_context={"pipeline": req.pipeline, "artifact": req.pipeline},
            budget=token_budget() or None,
            should_stop=lambda: run_id in _CANCELS,
        )

        stats = tracer.stats()
        status = "cancelled" if run_id in _CANCELS else (
            "succeeded" if not result.issues else "failed"
        )
        STORE.update(
            run_id,
            status=status,
            steps=stats["steps"],
            tokens_in=stats["tokens_in"],
            tokens_out=stats["tokens_out"],
            files=result.written,
            issues=result.issues,
            output=result.output[:4000],
        )
    except Exception as exc:  # noqa: BLE001
        STORE.update(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        _CANCELS.discard(run_id)
