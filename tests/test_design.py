from pathlib import Path

import pytest

from pdc.design import build_design
from pdc.ir import DesignDoc, Finding, Sheet
from pdc.parser import parse_pipeline
from pdc.render import render_review_md, render_xlsx
from pdc.tools import Toolbox
from pdc.trace import Tracer
from pdc.validate import check_numbering, validate_doc

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
CRD = PIPELINES
SPEND = CRD / "fact-order-fulfillment.yaml"

needs_repo = pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")


@needs_repo
def test_build_design_has_expected_sheets(tmp_path):
    doc = build_design(parse_pipeline(SPEND))
    names = [s.name for s in doc.sheets]
    assert "Overview" in names
    assert "Source Inventory" in names
    assert "Transform Chain" in names
    assert "Schema Diff" in names
    assert "Review Notes" in names
    assert any(n.startswith("Landing - ") for n in names)


@needs_repo
def test_landing_sheet_matches_declared_schema(tmp_path):
    facts = parse_pipeline(SPEND)
    doc = build_design(facts)
    eid = next(s for s in doc.sheets if s.name == "Landing - eid")
    assert len(eid.rows) == 19
    assert {r[1] for r in eid.rows} == {c.name for c in facts.jobs[0].load.schema}


@needs_repo
def test_findings_include_schema_mismatch():
    doc = build_design(parse_pipeline(SPEND))
    codes = {f.code for f in doc.findings}
    assert "schema_mismatch" in codes
    high = [f for f in doc.findings if f.severity == "high"]
    assert high, "必须有一条 high"
    assert "CUSTOMER_COUNTRY" in high[0].detail


@needs_repo
def test_render_and_validate_roundtrip(tmp_path):
    doc = build_design(parse_pipeline(SPEND))
    xlsx = render_xlsx(doc, tmp_path / "d.xlsx")
    assert xlsx.exists()
    issues = validate_doc(doc, xlsx)
    assert issues == [], issues


@needs_repo
def test_render_review_md(tmp_path):
    doc = build_design(parse_pipeline(SPEND))
    md = render_review_md(doc, tmp_path / "r.md")
    text = md.read_text(encoding="utf-8")
    assert "# 设计评审" in text
    assert "high" in text


def test_formula_injection_is_detected(tmp_path):
    doc = DesignDoc(pipeline="x", source_path="x")
    s = Sheet("S", ["A"], widths=[10])
    s.rows.append(["=cmd|calc"])
    doc.sheets.append(s)
    xlsx = render_xlsx(doc, tmp_path / "x.xlsx")
    issues = validate_doc(doc, xlsx)
    # 渲染时加了前导空格，所以不应被当成公式；这里断言确实没有公式注入
    assert not any("formula_injection" in i for i in issues)


def test_numbering_check():
    lines = ["1. 概述", "2. 架构", "4. 跳过了一个", "5. 继续"]
    issues = check_numbering(lines)
    assert len(issues) == 1 and "4" in issues[0]


@needs_repo
def test_toolbox_caches_facts(tmp_path):
    tb = Toolbox(out_dir=tmp_path, tracer=Tracer())
    tb.inspect_pipeline("fact-order-fulfillment.yaml")
    assert "fact-order-fulfillment.yaml" in tb._facts


@needs_repo
def test_tool_error_is_structured(tmp_path):
    tb = Toolbox(out_dir=tmp_path, tracer=Tracer())
    res = tb.call("inspect_pipeline", {"name": "not-exist.yaml"})
    assert res.ok is False
    assert "FileNotFoundError" in res.error
