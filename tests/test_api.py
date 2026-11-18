from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pdc.api as api

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
CRD = PIPELINES
NAME = "fact-order-fulfillment.yaml"
needs_repo = pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")

client = TestClient(api.app)


def test_health_exposes_capacity():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["max_concurrency"] >= 1
    assert body["in_flight"] == 0


@needs_repo
def test_create_run_offline():
    r = client.post("/runs", json={"pipeline": NAME, "mode": "offline"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["steps"] > 0
    assert body["files"]
    assert body["issues"] == []

    # 生成的文件真的存在
    assert Path(body["files"]["xlsx"]).exists()


def test_auto_mode_requires_model():
    r = client.post("/runs", json={"pipeline": NAME, "mode": "auto"})
    assert r.status_code == 400


def test_unknown_run_returns_404():
    assert client.get("/runs/does-not-exist").status_code == 404


def test_overload_returns_429(monkeypatch):
    """占满并发槽之后，新请求必须快速失败，而不是排队。"""

    def slow_execute(*_a, **_kw):
        time.sleep(0.4)
        return {
            "run_id": "x", "pipeline": "p", "output": "", "steps": 0,
            "files": {}, "issues": [], "stats": {},
        }

    monkeypatch.setattr(api, "_execute", slow_execute)
    monkeypatch.setattr(api, "_runner", api.BoundedRunner(limit=1, on_full="reject"))

    results: list[int] = []

    def fire():
        r = client.post("/runs", json={"pipeline": "p", "mode": "offline"})
        results.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert 429 in results, f"应当有请求被拒，实际 {results}"
    assert 200 in results, f"至少一个应当成功，实际 {results}"
    assert all(code in (200, 429) for code in results)


@needs_repo
def test_stream_emits_steps_then_done():
    """SSE：至少要有 tool/llm 事件，最后一行必须是 done。"""
    body = ""
    with client.stream("GET", "/runs/stream", params={"pipeline": NAME, "mode": "offline"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for chunk in r.iter_lines():
            if chunk:
                body += chunk + "\n"

    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert events, "没有收到任何事件"
    assert any(e.get("kind") == "tool" for e in events)
    assert events[-1]["kind"] == "done"
    assert events[-1]["files"]
