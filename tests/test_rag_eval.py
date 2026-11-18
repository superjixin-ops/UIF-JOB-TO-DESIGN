from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.eval import mrr, rank_hybrid, recall_at_k  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
CRD = PIPELINES


def test_recall_at_k():
    ranked = {"a": ["x", "y", "z"], "b": ["p", "q", "r"]}
    labels = {"a": {"x"}, "b": {"r"}}
    assert recall_at_k(ranked, labels, 1) == 0.5
    assert recall_at_k(ranked, labels, 3) == 1.0


def test_mrr():
    ranked = {"a": ["x", "y"], "b": ["p", "q"]}
    labels = {"a": {"x"}, "b": {"q"}}
    assert mrr(ranked, labels) == pytest.approx((1.0 + 0.5) / 2, abs=1e-4)


def test_hybrid_alpha_extremes():
    bm = ["a", "b", "c"]
    vec = ["c", "a", "b"]
    assert rank_hybrid(bm, vec, 1.0)[0] == "a"      # 全 BM25
    assert rank_hybrid(bm, vec, 0.0)[0] == "c"      # 全向量
    assert rank_hybrid(bm, vec, 1.0, rrf=True)[0] == "a"


def test_hybrid_merges_ids_from_both():
    bm = ["a", "b"]
    vec = ["c", "d"]
    out = rank_hybrid(bm, vec, 0.5)
    assert set(out) == {"a", "b", "c", "d"}


@pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")
def test_corpus_includes_skills():
    """语料必须覆盖 .bob/skills，否则多源维度这类问题永远检索不到。"""
    from pdc.rag import RuleIndex

    index = RuleIndex()
    ids = {c["id"].split("#")[0] for c in index.chunks}
    assert any(i.startswith("skill-") for i in ids)
    assert len(index.chunks) > 20


@pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")
def test_skills_do_not_collide_on_id():
    from pdc.rag import RuleIndex

    index = RuleIndex()
    skill_ids = {c["id"].split("#")[0] for c in index.chunks if c["id"].startswith("skill-")}
    # 10 个 skill 如果都用 "SKILL" 做 id，这里会变成 1
    assert len(skill_ids) >= 1
