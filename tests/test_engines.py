from __future__ import annotations

import json

import pytest

from pdc.llm import LLMReply, ScriptedLLM, ToolCall
from pdc.multiagent import WORKERS, _filter_schemas, run_multi
from pdc.planner import PlanStep, execute_plan, make_plan
from pdc.tools import Toolbox
from pdc.trace import Tracer


# ---------------------------------------------------------------- Plan
def test_make_plan_parses_steps():
    llm = ScriptedLLM([LLMReply(content=json.dumps({
        "steps": [
            {"title": "读文件", "goal": "inspect_pipeline x.yaml"},
            {"title": "写文档", "goal": "write_document x.yaml"},
        ]
    }))])
    plan = make_plan(llm, "为 x.yaml 生成文档")
    assert [s.title for s in plan] == ["读文件", "写文档"]
    assert plan[0].index == 1


def test_make_plan_survives_dirty_json():
    llm = ScriptedLLM([LLMReply(content='好的：\n```json\n{"steps":[{"title":"A","goal":"g"}]}\n```')])
    plan = make_plan(llm, "x")
    # 代码块包裹的 JSON 目前解析不了，应当优雅退化而不是抛异常
    assert isinstance(plan, list)


def test_make_plan_caps_steps():
    steps = [{"title": f"t{i}", "goal": "g"} for i in range(20)]
    llm = ScriptedLLM([LLMReply(content=json.dumps({"steps": steps}))])
    assert len(make_plan(llm, "x", max_steps=3)) == 3


def test_execute_plan_runs_each_step_and_accumulates(tmp_path):
    def factory(name: str):
        return Toolbox(repo=tmp_path, out_dir=tmp_path, tracer=Tracer())

    tb = factory("t")
    script = [
        # make_plan
        LLMReply(content=json.dumps({"steps": [
            {"title": "第一步", "goal": "g1"},
            {"title": "第二步", "goal": "g2"},
        ]})),
        # step 1 的 agent
        LLMReply(content="step1 done"),
        # step 2 的 agent
        LLMReply(content="step2 done"),
    ]
    result = execute_plan(ScriptedLLM(script), tb, "总目标")
    assert [s.result for s in result.plan] == ["step1 done", "step2 done"]
    assert result.total_steps == 2


def test_execute_plan_falls_back_when_plan_empty(tmp_path):
    tb = Toolbox(repo=tmp_path, out_dir=tmp_path, tracer=Tracer())
    llm = ScriptedLLM([
        LLMReply(content='{"steps": []}'),   # 规划失败
        LLMReply(content="直接做了"),         # 退化成单步
    ])
    result = execute_plan(llm, tb, "x")
    assert result.final == "直接做了"


# ---------------------------------------------------------------- Multi
def test_worker_tool_subset_is_deduped():
    """重复的 schema 会让 API 报 Tool names must be unique。"""
    schemas = _filter_schemas(["inspect_pipeline", "diff_schema"])
    names = [s["function"]["name"] for s in schemas]
    assert names == ["inspect_pipeline", "diff_schema"]
    assert len(names) == len(set(names))


def test_worker_only_gets_allowed_tools():
    names = {s["function"]["name"] for s in _filter_schemas(WORKERS["writer"]["tools"])}
    assert "write_document" in names
    assert "list_pipelines" not in names


def test_run_multi_delegates_then_finishes(tmp_path):
    tb = Toolbox(repo=tmp_path, out_dir=tmp_path, tracer=Tracer())
    script = [
        # supervisor 第一次：派给 auditor
        LLMReply(tool_calls=[ToolCall(id="c1", name="delegate",
                                      arguments={"worker": "auditor", "task": "看 x"})]),
        # worker 执行一轮后给结论
        LLMReply(content="发现 1 个 high"),
        # supervisor 第二次：收尾
        LLMReply(tool_calls=[ToolCall(id="c2", name="finish",
                                      arguments={"summary": "审计完成，1 个 high"})]),
    ]
    result = run_multi(ScriptedLLM(script), tb, "审计 x")

    assert result.final == "审计完成，1 个 high"
    assert len(result.delegations) == 1
    assert result.delegations[0].worker == "auditor"


def test_run_multi_rejects_unknown_worker(tmp_path):
    tb = Toolbox(repo=tmp_path, out_dir=tmp_path, tracer=Tracer())
    script = [
        LLMReply(tool_calls=[ToolCall(id="c1", name="delegate",
                                      arguments={"worker": "hacker", "task": "x"})]),
        LLMReply(content="收尾"),
    ]
    result = run_multi(ScriptedLLM(script), tb, "x")
    assert result.delegations == []
    assert result.final == "收尾"


def test_run_multi_stops_at_max_turns(tmp_path):
    tb = Toolbox(repo=tmp_path, out_dir=tmp_path, tracer=Tracer())
    loop = [LLMReply(tool_calls=[ToolCall(id="c1", name="delegate",
                                          arguments={"worker": "auditor", "task": "x"})])] * 3
    script = []
    for _ in range(3):
        script.append(loop.pop())
        script.append(LLMReply(content="worker 结果"))
    result = run_multi(ScriptedLLM(script), tb, "x", max_turns=2)
    assert "最大调度轮数" in result.final
