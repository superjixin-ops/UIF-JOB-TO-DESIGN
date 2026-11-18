from __future__ import annotations

from pathlib import Path

import pytest

from pdc.audit import AuditResult, render_report, verify_findings
from pdc.llm import LLMReply, ScriptedLLM
from pdc.tools import Toolbox
from pdc.trace import Tracer

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
FULFILLMENT = PIPELINES / "fact-order-fulfillment.yaml"


@pytest.fixture
def toolbox(tmp_path) -> Toolbox:
    return Toolbox(repo=PIPELINES, out_dir=tmp_path, tracer=Tracer())


# ---------------------------------------------------------------- 工具约束
def test_record_finding_rejects_bad_severity(toolbox):
    with pytest.raises(ValueError, match="severity"):
        toolbox.record_finding("critical", "t", "d", basis="file")


def test_record_finding_rejects_bad_basis(toolbox):
    with pytest.raises(ValueError, match="basis"):
        toolbox.record_finding("high", "t", "d", basis="guess")


def test_record_finding_rejects_missing_evidence(toolbox):
    with pytest.raises(FileNotFoundError):
        toolbox.record_finding("high", "t", "d", evidence="/nope/x.yaml", basis="file")


def test_record_finding_accepts_real_evidence(toolbox):
    res = toolbox.record_finding(
        "high", "标题", "细节", evidence=str(FULFILLMENT), basis="tool"
    )
    assert res["recorded"] == 1
    assert toolbox.audit_findings[0]["basis"] == "tool"


def test_finish_audit_records_summary(toolbox):
    toolbox.inspect_pipeline("fact-order-fulfillment.yaml")
    res = toolbox.finish_audit("覆盖 1 个文件")
    assert res["inspected_files"] == ["fact-order-fulfillment.yaml"]
    assert toolbox.audit_summary == "覆盖 1 个文件"


def test_inspect_records_visited_files(toolbox):
    toolbox.inspect_pipeline("fact-order-fulfillment.yaml")
    toolbox.inspect_pipeline("dim-customer-segment.yaml")
    assert toolbox.inspected == ["fact-order-fulfillment.yaml", "dim-customer-segment.yaml"]


def test_audit_schemas_only_when_requested():
    names = [t["function"]["name"] for t in Toolbox.schemas(include_audit=True)]
    assert "record_finding" in names
    assert "finish_audit" in names

    plain = [t["function"]["name"] for t in Toolbox.schemas()]
    assert "record_finding" not in plain


# ---------------------------------------------------------------- critic
def test_verify_skips_tool_basis():
    """工具算出的结论不该被模型复核——它读原文只会数错。"""
    findings = [{"severity": "high", "title": "7 列未声明", "detail": "d",
                 "evidence": str(FULFILLMENT), "basis": "tool"}]
    llm = ScriptedLLM([])          # 一次都不该调用
    kept, rejected = verify_findings(llm, findings)

    assert len(kept) == 1 and not rejected
    assert llm.calls == []
    assert "跳过模型复核" in kept[0]["verify_reason"]


def test_verify_rejects_unsupported_claim():
    findings = [{"severity": "check", "title": "缺 keyName", "detail": "d",
                 "evidence": str(FULFILLMENT), "basis": "file"}]
    llm = ScriptedLLM([LLMReply(content='{"supported": false, "reason": "文件里有 keyName"}')])
    kept, rejected = verify_findings(llm, findings)

    assert not kept and len(rejected) == 1
    assert "文件里有" in rejected[0]["verify_reason"]


def test_verify_keeps_supported_claim():
    findings = [{"severity": "check", "title": "T", "detail": "d",
                 "evidence": str(FULFILLMENT), "basis": "file"}]
    llm = ScriptedLLM([LLMReply(content='{"supported": true, "reason": "有依据"}')])
    kept, rejected = verify_findings(llm, findings)

    assert len(kept) == 1 and not rejected


def test_verify_rejects_without_evidence():
    findings = [{"severity": "high", "title": "T", "detail": "d",
                 "evidence": "", "basis": "file"}]
    kept, rejected = verify_findings(ScriptedLLM([]), findings)
    assert not kept and "没有可核查的证据" in rejected[0]["verify_reason"]


# ---------------------------------------------------------------- 报告
def test_report_separates_rejected(tmp_path):
    result = AuditResult(
        summary="覆盖完毕",
        findings=[{"severity": "high", "title": "真问题", "detail": "d", "evidence": ""}],
        rejected=[{"severity": "check", "title": "被驳回", "detail": "d",
                   "verify_reason": "文件里其实有"}],
        inspected=["a.yaml"],
        steps=5,
    )
    out = render_report(result, PIPELINES, tmp_path / "r.md")
    text = out.read_text(encoding="utf-8")

    assert "真问题" in text
    assert "自检驳回" in text
    assert "文件里其实有" in text
