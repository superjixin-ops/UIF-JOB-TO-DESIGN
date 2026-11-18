"""FastAPI 服务层。

四个端点：
  POST /runs          发起一次生成（有界并发，满了返回 429）
  GET  /runs/{id}     取结果
  GET  /runs/stream   SSE 流式推送每一步 trace
  GET  /health        健康检查 + 当前并发状态

并发策略：on_full="reject"。
在线请求宁可 429 让客户端重试，也不让 p99 因为排队而雪崩。
后台批处理应该用 on_full="wait"，那是另一种场景（见 loadtest）。
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pdc.agent import run_with_self_check
from pdc.concurrency import BoundedRunner, BusyError
from pdc.llm import OpenAICompatibleLLM, RuleBasedLLM
from pdc.paths import describe, out_dir
from pdc.tools import Toolbox
from pdc.trace import Tracer

app = FastAPI(title="Pipeline Design Copilot", version="0.3.0")

# 生成的 xlsx / review.md 直接可下载：/files/xxx-design.xlsx
app.mount("/files", StaticFiles(directory=str(out_dir())), name="files")

MAX_CONCURRENCY = int(os.getenv("PDC_MAX_CONCURRENCY", "4"))
_runner = BoundedRunner(limit=MAX_CONCURRENCY, on_full="reject")
_RUNS: dict[str, dict] = {}


class RunRequest(BaseModel):
    pipeline: str = Field(min_length=1)
    mode: str = Field(default="offline", pattern="^(offline|auto)$")
    model: str = ""
    max_steps: int = 15


class RunResponse(BaseModel):
    run_id: str
    pipeline: str
    output: str
    steps: int
    files: dict
    issues: list[str]
    stats: dict


def _build_llm(mode: str, model: str):
    if mode == "auto":
        if not model:
            raise HTTPException(status_code=400, detail="auto 模式必须提供 model")
        return OpenAICompatibleLLM(model)
    return RuleBasedLLM()


def _execute(pipeline: str, mode: str, model: str, max_steps: int, tracer: Tracer) -> dict:
    """在 worker 线程里跑完整 Agent。"""
    toolbox = Toolbox(out_dir=out_dir(), tracer=tracer)
    goal = (
        f"为 pipeline `{pipeline}` 生成设计文档。\n"
        "1. 先用工具获取事实\n2. 列数/差异必须调用 diff_schema\n"
        "3. 生成后自检，有问题就修\n4. 汇报文件路径和 high 级问题数"
    )
    result = run_with_self_check(
        _build_llm(mode, model),
        toolbox,
        goal,
        max_steps=max_steps,
        user_context={"pipeline": pipeline, "artifact": pipeline},
    )
    return {
        "run_id": tracer.run_id,
        "pipeline": pipeline,
        "output": result.output,
        "steps": tracer.stats()["steps"],
        "files": result.written,
        "issues": result.issues,
        "stats": tracer.stats(),
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "max_concurrency": MAX_CONCURRENCY,
        "in_flight": _runner.in_flight,
        "runs_recorded": len(_RUNS),
        "paths": describe(),
    }


@app.post("/runs", response_model=RunResponse, responses={429: {"description": "并发已满"}})
async def create_run(req: RunRequest) -> RunResponse:
    tracer = Tracer(path=out_dir() / f"{req.pipeline.replace('.yaml', '')}-trace.jsonl")
    try:
        payload = await _runner.run(_execute, req.pipeline, req.mode, req.model, req.max_steps, tracer)
    except BusyError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "2"},
        ) from exc

    _RUNS[payload["run_id"]] = payload
    return RunResponse(**payload)


@app.get("/runs/stream")
async def stream_run(
    pipeline: str = Query(min_length=1),
    mode: str = Query(default="offline", pattern="^(offline|auto)$"),
    model: str = "",
    max_steps: int = 15,
) -> StreamingResponse:
    """SSE：每完成一步就推一条，前端可以立刻画出时间线。

    典型收益：首字节 300ms 而不是等 12 秒。
    """

    async def gen():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_step(step) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, step)

        tracer = Tracer(
            path=out_dir() / f"{pipeline.replace('.yaml', '')}-trace.jsonl",
            on_step=on_step,
        )
        task = asyncio.create_task(
            asyncio.to_thread(_execute, pipeline, mode, model, max_steps, tracer)
        )

        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    step = await asyncio.wait_for(queue.get(), timeout=0.2)
                except TimeoutError:
                    continue
                yield "data: " + json.dumps(
                    {
                        "index": step.index,
                        "kind": step.kind,
                        "name": step.name,
                        "latency_ms": step.latency_ms,
                        "error": step.error,
                    },
                    ensure_ascii=False,
                ) + "\n\n"

            if task.exception():
                yield "data: " + json.dumps(
                    {"kind": "error", "message": str(task.exception())}, ensure_ascii=False
                ) + "\n\n"
            else:
                result = task.result()
                _RUNS[result["run_id"]] = result
                yield "data: " + json.dumps(
                    {"kind": "done", "run_id": result["run_id"], "files": result["files"]},
                    ensure_ascii=False,
                ) + "\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail="run 不存在")
    return _RUNS[run_id]
