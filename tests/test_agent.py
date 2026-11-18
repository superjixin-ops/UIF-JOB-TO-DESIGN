"""Agent 循环测试。用脚本模型驱动，不碰网络。

这三条用例证明的是"循环逻辑正确"，不是"模型聪明"。
区分这两件事，是工程和玄学的分界线。
"""

from pathlib import Path

import pytest

from pdc.agent import run_agent, run_with_self_check
from pdc.llm import LLMReply, ScriptedLLM, ToolCall
from pdc.tools import Toolbox
from pdc.trace import Tracer

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
CRD = PIPELINES
NAME = "fact-order-fulfillment.yaml"

needs_repo = pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")


def _call(tool: str, **args) -> ToolCall:
    return ToolCall(id=f"c_{tool}", name=tool, arguments=args)


@needs_repo
def test_loop_executes_tools_then_stops(tmp_path):
    script = [
        LLMReply(tool_calls=[_call("inspect_pipeline", name=NAME)]),
        LLMReply(tool_calls=[_call("diff_schema", name=NAME, job_index=0)]),
        LLMReply(content="完成，发现 1 个高风险问题。"),
    ]
    llm = ScriptedLLM(script)
    tb = Toolbox(out_dir=tmp_path, tracer=Tracer())

    result = run_agent(llm, tb, f"为 {NAME} 生成设计文档")

    assert result.steps == 3
    assert "完成" in result.output
    kinds = [s.kind for s in result.trace.steps]
    assert kinds.count("tool") == 2
    assert kinds.count("llm") == 3
    # 第二次调用时，模型应该能看到第一次工具的结果
    assert any("ok" in str(m) for m in llm.calls[-1])


@needs_repo
def test_tool_failure_is_fed_back_not_crashing(tmp_path):
    script = [
        LLMReply(tool_calls=[_call("inspect_pipeline", name="does-not-exist.yaml")]),
        LLMReply(content="工具报错了，我需要换一个名字。"),
    ]
    llm = ScriptedLLM(script)
    tb = Toolbox(out_dir=tmp_path, tracer=Tracer())
    result = run_agent(llm, tb, "test")

    assert result.steps == 2
    assert result.trace.stats()["errors"] == 1
    assert "换一个名字" in result.output


@needs_repo
def test_max_steps_is_enforced(tmp_path):
    # 模型永远请求工具，从不收尾
    script = [LLMReply(tool_calls=[_call("inspect_pipeline", name=NAME)]) for _ in range(10)]
    llm = ScriptedLLM(script)
    tb = Toolbox(out_dir=tmp_path, tracer=Tracer())
    result = run_agent(llm, tb, "loop forever", max_steps=3)

    assert result.steps == 3
    assert "最大步数" in result.output


@needs_repo
def test_full_pipeline_offline_is_clean(tmp_path):
    """完整 SOP：inspect -> diff -> build -> write -> validate。"""
    script = [
        LLMReply(tool_calls=[_call("inspect_pipeline", name=NAME)]),
        LLMReply(tool_calls=[_call("diff_schema", name=NAME, job_index=0)]),
        LLMReply(tool_calls=[_call("build_design", name=NAME)]),
        LLMReply(tool_calls=[_call("write_document", name=NAME)]),
        LLMReply(tool_calls=[_call("validate_document", name=NAME)]),
        LLMReply(content="生成完成，自检通过。"),
    ]
    tb = Toolbox(out_dir=tmp_path, tracer=Tracer())
    result = run_with_self_check(ScriptedLLM(script), tb, "生成设计文档")

    assert result.issues == []
    assert result.written.get("xlsx")
    assert Path(result.written["xlsx"]).exists()
    assert Path(result.written["review"]).exists()


class FlakyToolbox(Toolbox):
    """第一次校验故意失败，第二次通过。用来测重试逻辑本身。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.validate_calls = 0

    def validate_document(self, name: str) -> dict:
        self.validate_calls += 1
        if self.validate_calls == 1:
            return {"issues": ["[header_mismatch] 故意制造的问题"], "clean": False}
        return {"issues": [], "clean": True}


@needs_repo
def test_self_check_retries_on_issues(tmp_path):
    """第一轮校验失败 -> 自动再跑一轮 -> 第二轮通过。"""
    script = [
        LLMReply(tool_calls=[_call("write_document", name=NAME)]),
        LLMReply(tool_calls=[_call("validate_document", name=NAME)]),
        LLMReply(content="第一轮失败"),
        LLMReply(tool_calls=[_call("write_document", name=NAME)]),
        LLMReply(tool_calls=[_call("validate_document", name=NAME)]),
        LLMReply(content="第二轮通过"),
    ]
    tb = FlakyToolbox(out_dir=tmp_path, tracer=Tracer())
    result = run_with_self_check(ScriptedLLM(script), tb, "生成", max_rounds=2)

    assert tb.validate_calls == 2
    assert result.issues == []
    assert result.written
    assert "第二轮通过" in result.output
