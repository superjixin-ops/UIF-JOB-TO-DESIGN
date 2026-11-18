"""幻觉检测的测试。

最重要的不是"能不能检出"，而是"能不能检出我故意埋的假话"。
所以这里的用例都是伪造的 Finding —— 每一条都对应一种真实幻觉模式。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdc.design import build_design
from pdc.grounding import GroundingChecker, ground, grounding_score
from pdc.ir import DesignDoc, Finding
from pdc.parser import parse_pipeline

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
CRD = PIPELINES
SPEND = CRD / "fact-order-fulfillment.yaml"
RULES = SAMPLES / "rules"

needs_repo = pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")


def _doc_with(*findings: Finding) -> DesignDoc:
    return DesignDoc(pipeline="t", source_path="t", findings=list(findings))


@needs_repo
def test_real_findings_are_grounded():
    """工具生成的文档，必须 100% 能对上事实。"""
    facts = parse_pipeline(SPEND)
    doc = build_design(facts)
    rule_ids = {p.stem for p in RULES.glob("*.md")} if RULES.exists() else set()

    violations = ground(doc, facts, rule_ids)
    assert violations == [], "\n".join(map(str, violations))
    assert grounding_score(violations, len(doc.findings)) == 1.0


@needs_repo
def test_unknown_rule_is_caught():
    facts = parse_pipeline(SPEND)
    doc = _doc_with(
        Finding(
            code="x", severity="check", title="T", detail="D",
            rule_id="99-rule-that-does-not-exist",   # 编的
        )
    )
    v = ground(doc, facts, {"03-fact-pipeline-structure-requirements"})
    assert any(x.code == "unknown_rule" for x in v)


@needs_repo
def test_invented_column_is_caught():
    """模型最爱干的事：编一个看起来很合理的列名。"""
    facts = parse_pipeline(SPEND)
    real = facts.jobs[0].load.schema[0].name
    doc = _doc_with(
        Finding(
            code="x", severity="high", title="列不一致",
            detail=f"{real} 与 MADE_UP_COLUMN_NAME 不匹配",
        )
    )
    v = ground(doc, facts)
    assert any(x.code == "ungrounded_column" and "MADE_UP_COLUMN_NAME" in x.message for x in v)


@needs_repo
def test_number_mismatch_is_caught():
    """声称 9 列，实际只列出 3 个。"""
    facts = parse_pipeline(SPEND)
    doc = _doc_with(
        Finding(
            code="schema_mismatch", severity="high", title="T",
            detail="9 列在数据帧里但 schema 未声明: ['A_ONE', 'B_TWO', 'C_THREE']",
        )
    )
    v = ground(doc, facts)
    assert any(x.code == "number_mismatch" for x in v)


@needs_repo
def test_missing_evidence_is_caught():
    facts = parse_pipeline(SPEND)
    doc = _doc_with(
        Finding(code="x", severity="info", title="T", detail="D",
                evidence="/nope/does-not-exist.yaml")
    )
    v = ground(doc, facts)
    assert any(x.code == "missing_evidence" for x in v)


@needs_repo
def test_grounding_score_penalises_hallucination():
    facts = parse_pipeline(SPEND)
    doc = _doc_with(
        Finding(code="a", severity="high", title="真实", detail="正确"),
        Finding(code="b", severity="high", title="编造",
                detail="出现了 FAKE_COLUMN_XYZ 这个列"),
    )
    v = ground(doc, facts)
    score = grounding_score(v, len(doc.findings))
    assert score == 0.5


def test_empty_doc_scores_perfect():
    doc = DesignDoc(pipeline="t", source_path="t")
    assert grounding_score([], 0) == 1.0


@needs_repo
def test_finding_without_rule_id_is_allowed():
    """没有 rule_id 不算幻觉，只是没有依据 —— 不能误判。"""
    facts = parse_pipeline(SPEND)
    doc = _doc_with(Finding(code="no_transform", severity="info", title="T", detail="D"))
    assert ground(doc, facts, {"any"}) == []
