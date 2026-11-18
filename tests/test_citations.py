from pathlib import Path

import pytest

from pdc.citations import RuleCitations

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
RULES = SAMPLES / "rules"
needs_rules = pytest.mark.skipif(not RULES.exists(), reason="samples/rules 缺失")


@pytest.fixture
def rc() -> RuleCitations:
    return RuleCitations()


@needs_rules
def test_real_citations_pass(rc):
    """DeepSeek 第一次运行里真实引用过的三个锚点，必须全部通过。"""
    text = (
        "按规则 02-load-schema#Schema drift "
        "和 01-pipeline-structure#Extract rules，"
        "以及 04-sizing#The tiers 的要求。"
    )
    assert rc.check(text) == []


@needs_rules
def test_unknown_file_is_caught(rc):
    problems = rc.check("参考 99-does-not-exist#Whatever")
    assert len(problems) == 1
    assert "citation_file" in problems[0]


@needs_rules
def test_fabricated_anchor_is_caught(rc):
    """最危险的幻觉：文件名真实，锚点编的。"""
    problems = rc.check("按 02-load-schema#Schema Drift Detection Policy For Production 的说明")
    assert len(problems) == 1
    assert "citation_anchor" in problems[0]


@needs_rules
def test_fabricated_section_number_is_caught(rc):
    problems = rc.check("见 01-pipeline-structure#99")
    assert len(problems) == 1
    assert "citation_anchor" in problems[0]


@needs_rules
def test_no_citation_no_problem(rc):
    assert rc.check("这段没有任何引用。") == []


@needs_rules
def test_truncated_anchor_is_accepted(rc):
    """真实标题是 "Schema drift"，模型写 "Schema" —— 属截断，不该报错。"""
    assert rc.check("见 02-load-schema#Schema") == []


@needs_rules
def test_embellished_anchor_is_rejected(rc):
    """反过来：比真实标题更长更具体，就是编造。"""
    problems = rc.check("见 02-load-schema#Schema Drift Detection Policy")
    assert len(problems) == 1
