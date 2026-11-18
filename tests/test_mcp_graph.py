from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pdc.langgraph_engine import build_graph, make_checkpointer, run_graph
from pdc.llm import LLMReply, ScriptedLLM, ToolCall
from pdc.mcp_client import MCPClient, MCPError
from pdc.mcp_server import handle
from pdc.tools import Toolbox
from pdc.trace import Tracer

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"


# ---------------------------------------------------------------- MCP server
def test_server_lists_tools():
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"find_pipelines", "pipeline_stats"} <= names


def test_server_initialize():
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["serverInfo"]["name"]


def test_server_unknown_method():
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert resp["error"]["code"] == -32601


def test_server_tool_error_is_content_not_crash():
    resp = handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "pipeline_stats", "arguments": {"path": "/nope.yaml"}},
    })
    assert resp["result"]["isError"] is True
    assert "FileNotFoundError" in resp["result"]["content"][0]["text"]


def test_server_notification_has_no_response():
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# ---------------------------------------------------------------- MCP client
@pytest.fixture
def client():
    c = MCPClient([sys.executable, "-m", "pdc.mcp_server"]).start()
    yield c
    c.close()


def test_client_handshake_and_list(client):
    assert client.server_info["name"] == "pdc-fdc" or client.server_info["name"]
    names = [t["name"] for t in client.tools]
    assert "mcp_find_pipelines" in names


def test_client_call_returns_text(client):
    out = client.call("mcp_find_pipelines", {"directory": str(PIPELINES)})
    assert out["ok"]
    assert json.loads(out["text"])["count"] >= 3


def test_client_reports_tool_error(client):
    out = client.call("mcp_pipeline_stats", {"path": "/nope.yaml"})
    assert out["ok"] is False
    assert "FileNotFoundError" in out["text"]


def test_schemas_are_openai_compatible(client):
    schemas = client.tool_schemas()
    for s in schemas:
        assert s["type"] == "function"
        assert s["function"]["name"].startswith("mcp_")
        assert "parameters" in s["function"]


def test_unknown_server_command_raises():
    c = MCPClient(["definitely-not-a-command-xyz"], timeout=3)
    with pytest.raises((MCPError, FileNotFoundError, OSError)):
        c.start()


# ---------------------------------------------------------------- 接入 Toolbox
def test_toolbox_dispatches_to_mcp(client, tmp_path):
    tb = Toolbox(repo=PIPELINES, out_dir=tmp_path, tracer=Tracer())
    tb.attach_mcp(client)

    names = {s["function"]["name"] for s in tb.tool_schemas()}
    assert "mcp_pipeline_stats" in names
    assert "inspect_pipeline" in names          # 本地工具还在

    res = tb.call("mcp_find_pipelines", {"directory": str(PIPELINES)})
    assert res.ok
    assert "files" in str(res.data)


def test_toolbox_without_mcp_rejects_prefix(tmp_path):
    tb = Toolbox(repo=PIPELINES, out_dir=tmp_path, tracer=Tracer())
    res = tb.call("mcp_find_pipelines", {})
    assert res.ok is False
    assert "未知工具" in res.error


# ---------------------------------------------------------------- LangGraph
def test_graph_runs_tool_then_finishes(tmp_path):
    tb = Toolbox(repo=PIPELINES, out_dir=tmp_path, tracer=Tracer())
    script = [
        LLMReply(tool_calls=[ToolCall(id="c1", name="inspect_pipeline",
                                      arguments={"name": "fact-order-fulfillment.yaml"})]),
        LLMReply(content="看完了，有 7 列漂移"),
    ]
    out = run_graph(ScriptedLLM(script), tb, "检查 fact-order-fulfillment.yaml")

    assert out["output"] == "看完了，有 7 列漂移"
    assert out["steps"] == 2
    # system + user + assistant(tool_calls) + tool + assistant(final)
    assert len(out["messages"]) >= 5


def test_graph_respects_max_steps(tmp_path):
    tb = Toolbox(repo=PIPELINES, out_dir=tmp_path, tracer=Tracer())
    loop = [
        LLMReply(tool_calls=[ToolCall(id=f"c{i}", name="inspect_pipeline",
                                      arguments={"name": "fact-order-fulfillment.yaml"})])
        for i in range(10)
    ]
    out = run_graph(ScriptedLLM(loop), tb, "loop", max_steps=3)
    assert out["steps"] == 3


def test_graph_compile_has_two_nodes(tmp_path):
    tb = Toolbox(repo=PIPELINES, out_dir=tmp_path, tracer=Tracer())
    app = build_graph(ScriptedLLM([]), tb)
    assert set(app.get_graph().nodes) >= {"agent", "tools"}


def test_checkpointer_is_available():
    """checkpointer 是 LangGraph 相对手写循环的核心收益。"""
    cp = make_checkpointer()
    assert cp is not None
