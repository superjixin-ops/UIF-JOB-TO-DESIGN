from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.rag import HybridRetriever, RuleIndex, get_retriever  # noqa: E402
from rag.analyze import read_run, report, was_retrieved  # noqa: E402
from pdc.citations import RuleCitations  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
CRD = PIPELINES
needs_repo = pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")


@pytest.fixture
def index() -> RuleIndex:
    return RuleIndex()


def test_hybrid_disabled_is_not_degraded(index):
    """主动关闭 ≠ 降级。混淆这两个状态会让 A/B 测试误报。"""
    r = HybridRetriever(index, alpha=0.1, enabled=False)
    hits = r.search("schema", top_k=3)
    assert r.disabled is True
    assert r.degraded is False
    assert len(hits) == 3
    assert all("id" in h for h in hits)


@needs_repo
def test_hybrid_returns_scored_hits(index):
    r = HybridRetriever(index, alpha=0.1, enabled=False)
    hits = r.search("load schema", top_k=5)
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_alpha_bounds_do_not_crash(index):
    for alpha in (0.0, 0.5, 1.0):
        r = HybridRetriever(index, alpha=alpha, enabled=False)
        assert r.search("size", top_k=2)


def test_was_retrieved_prefix_match():
    retrieved = {"08-common-mistakes-and-corrections#Load Schema"}
    assert was_retrieved("08-common-mistakes-and-corrections#Load Schema", retrieved)
    assert was_retrieved("08-common-mistakes-and-corrections#Load", retrieved)
    assert not was_retrieved("08-common-mistakes-and-corrections#Joins", retrieved)


def test_report_counts_only_retrieved(tmp_path):
    trace = tmp_path / "t.jsonl"
    steps = [
        {"kind": "tool", "name": "search_rules", "input": {"query": "q"},
         "output": [{"id": "08-common-mistakes-and-corrections#Load Schema", "heading": "Load Schema"}]},
        {"kind": "llm", "output": "见 08-common-mistakes-and-corrections#Load Schema 和 99-nope#Missing"},
    ]
    trace.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps), encoding="utf-8")

    run = read_run(trace)
    r = report(run, RuleCitations())

    assert r["citations"] == 2
    assert r["citations_retrieved"] == 1
    assert r["retrieval_grounding"] == 0.5


@needs_repo
def test_get_retriever_respects_env(monkeypatch):
    monkeypatch.setenv("PDC_RAG_MODE", "bm25")
    r = get_retriever()
    assert r.disabled is True
    assert r.degraded is False
