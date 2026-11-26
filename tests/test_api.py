"""API 测试。覆盖四件事：鉴权、限流、异步任务、SSE。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pdc import api
from pdc.jobs import JobStore
from pdc.security import RateLimiter

CRD = Path.home() / "Documents/CRD-7"
NAME = "fact-order-fulfillment.yaml"
needs_repo = pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个用例独立的 job 库和限流器，避免互相污染。"""
    monkeypatch.setenv("PDC_API_TOKEN", TOKEN)
    monkeypatch.setattr(api, "STORE", JobStore(tmp_path / "jobs.db"))
    monkeypatch.setattr(api, "_limiter", RateLimiter(per_min=600, burst=10))
    yield


client = TestClient(api.app)


def wait_done(run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/runs/{run_id}", headers=AUTH)
        if r.status_code == 200 and r.json()["status"] in ("succeeded", "failed", "cancelled"):
            return r.json()
        time.sleep(0.05)
    raise AssertionError("任务超时")


# ---------------------------------------------------------------- 鉴权
def test_missing_token_is_401():
    assert client.get("/pipelines").status_code == 401


def test_wrong_token_is_401():
    r = client.get("/pipelines", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_valid_token_passes():
    assert client.get("/pipelines", headers=AUTH).status_code == 200


def test_health_is_public():
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["auth_required"] is True


def test_health_reports_budget_and_rate():
    body = client.get("/health").json()
    assert body["token_budget"] > 0
    assert body["rate_limit_per_min"] >= 1


# ---------------------------------------------------------------- 限流
def test_rate_limit_returns_429(monkeypatch):
    """令牌桶耗尽后必须 429，并给出 Retry-After。"""
    monkeypatch.setattr(api, "_limiter", RateLimiter(per_min=1, burst=1))
    codes = [client.get("/pipelines", headers=AUTH).status_code for _ in range(4)]
    assert 200 in codes
    assert 429 in codes, codes

    # 再打一次，确认 429 带 Retry-After
    r = client.get("/pipelines", headers=AUTH)
    if r.status_code == 429:
        assert "Retry-After" in r.headers


# ---------------------------------------------------------------- 异步任务
@needs_repo
def test_create_run_returns_202_and_completes():
    r = client.post("/runs", json={"pipeline": NAME, "mode": "offline"}, headers=AUTH)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "queued"

    record = wait_done(run_id)
    assert record["status"] == "succeeded"
    assert record["steps"] > 0
    assert record["files"]
    assert Path(record["files"]["xlsx"]).exists()


@needs_repo
def test_run_is_persisted_in_store():
    r = client.post("/runs", json={"pipeline": NAME, "mode": "offline"}, headers=AUTH)
    run_id = r.json()["run_id"]
    wait_done(run_id)
    # 模拟"进程重启后查询"：直接读库
    assert api.STORE.get(run_id) is not None
    assert api.STORE.get(run_id).status == "succeeded"


def test_unknown_run_404():
    assert client.get("/runs/nope", headers=AUTH).status_code == 404


def test_cancel_finished_run_is_noop():
    r = client.post("/runs", json={"pipeline": NAME, "mode": "offline"}, headers=AUTH)
    run_id = r.json()["run_id"]
    wait_done(run_id)
    out = client.post(f"/runs/{run_id}/cancel", headers=AUTH).json()
    assert out["status"] == "succeeded"


@needs_repo
def test_trace_endpoint_returns_steps():
    r = client.post("/runs", json={"pipeline": NAME, "mode": "offline"}, headers=AUTH)
    run_id = r.json()["run_id"]
    wait_done(run_id)

    body = client.get(f"/runs/{run_id}/trace", headers=AUTH).json()
    assert body["steps"]
    kinds = {s["kind"] for s in body["steps"]}
    assert "tool" in kinds


@needs_repo
def test_stream_ends_with_done():
    r = client.post("/runs", json={"pipeline": NAME, "mode": "offline"}, headers=AUTH)
    run_id = r.json()["run_id"]

    events: list[dict] = []
    with client.stream("GET", f"/runs/{run_id}/stream?token={TOKEN}") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
            if events and events[-1].get("kind") == "done":
                break

    assert events
    assert events[-1]["kind"] == "done"
    assert events[-1]["status"] in ("succeeded", "failed")


def test_admission_rejects_when_full(monkeypatch):
    """并发槽占满时直接 429，而不是排队。

    直接构造"已满"状态：TestClient 会等待后台任务，真实并发很难在测试里复现。
    """
    monkeypatch.setattr(api, "MAX_CONCURRENCY", 2)
    monkeypatch.setattr(api, "_active", 2)
    monkeypatch.setattr(api, "_limiter", RateLimiter(per_min=600, burst=10))

    r = client.post("/runs", json={"pipeline": "x", "mode": "offline"}, headers=AUTH)
    assert r.status_code == 429
    assert "并发已满" in r.text
    assert r.headers.get("Retry-After") == "2"


def test_admit_and_release_are_symmetric(monkeypatch):
    monkeypatch.setattr(api, "MAX_CONCURRENCY", 2)
    monkeypatch.setattr(api, "_active", 0)
    assert api._admit() is True
    assert api._admit() is True
    assert api._admit() is False        # 第三个被拒
    api._release()
    assert api._admit() is True
    api._release(); api._release()
    assert api._active == 0


# ---------------------------------------------------------------- 页面
def test_index_serves_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert "Pipeline Design Copilot" in r.text


# ---------------------------------------------------------------- build 方向
def _make_design(out_dir: Path) -> Path:
    from pdc.design import build_design
    from pdc.parser import parse_pipeline
    from pdc.render import render_xlsx

    facts = parse_pipeline("samples/pipelines/fact-order-fulfillment.yaml")
    return render_xlsx(build_design(facts), out_dir / "fact-order-fulfillment-design.xlsx")


def test_build_requires_model_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = client.post("/build", json={"design": "x.xlsx"}, headers=AUTH)
    assert r.status_code == 400
    assert "真实模型" in r.json()["detail"]


def test_build_missing_design_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(api, "OUT", tmp_path)
    r = client.post("/build", json={"design": "nope.xlsx"}, headers=AUTH)
    assert r.status_code == 404


def test_build_success_with_stubbed_model(tmp_path, monkeypatch):
    """走完整的接口链路，只把模型换成桩。

    这样测的是"接口 -> 后台线程 -> 状态机 -> files"这一段，
    而不是模型能力——模型能力由 eval 负责，两类测试不能混。
    """
    from pdc.yamlgen import BuildResult

    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(api, "OUT", tmp_path)
    _make_design(tmp_path)

    def fake_run_build(*args, **kwargs):
        return BuildResult(
            status="passed",
            yaml_path=str(tmp_path / "generated.yaml"),
            rounds=2,
            steps=5,
            passed=True,
        )

    monkeypatch.setattr(api, "run_build", fake_run_build)

    r = client.post(
        "/build",
        json={"design": "fact-order-fulfillment-design.xlsx", "max_rounds": 4},
        headers=AUTH,
    )
    assert r.status_code == 202
    rec = wait_done(r.json()["run_id"])
    assert rec["kind"] == "build"
    assert rec["status"] == "succeeded"
    assert rec["files"]["yaml"].endswith("generated.yaml")
    assert "rounds=2" in rec["output"]


def test_designs_endpoint_lists_xlsx(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "OUT", tmp_path)
    _make_design(tmp_path)
    r = client.get("/designs", headers=AUTH)
    assert r.status_code == 200
    names = [d["name"] for d in r.json()["designs"]]
    assert "fact-order-fulfillment-design.xlsx" in names


# ---------------------------------------------------------------- 内存上限
def test_live_buffer_is_bounded(monkeypatch):
    """_LIVE 不能无限增长：只保留最近 N 次运行。

    之前它只增不删，跑得越久内存越大（每次运行约 6~15 KB，
    1000 次就是 10 MB 级）。
    """
    monkeypatch.setattr(api, "_LIVE", {})
    for i in range(api._MAX_LIVE_RUNS + 5):
        api._live_start(f"r{i}")

    assert len(api._LIVE) == api._MAX_LIVE_RUNS
    assert "r0" not in api._LIVE                       # 最老的被丢掉
    assert f"r{api._MAX_LIVE_RUNS + 4}" in api._LIVE   # 最新的还在


def test_live_append_keeps_steps(monkeypatch):
    monkeypatch.setattr(api, "_LIVE", {})
    api._live_start("x")
    api._live_append("x", {"index": 1})
    api._live_append("x", {"index": 2})
    assert [s["index"] for s in api._LIVE["x"]] == [1, 2]
