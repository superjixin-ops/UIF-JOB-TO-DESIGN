from __future__ import annotations

from pathlib import Path

import pytest

from pdc.memory import Memory, SessionStore, Turn, estimate_tokens, recall
from pdc.rerank import rerank, score, tokens


# ---------------------------------------------------------------- 短期 + 持久化
def test_turn_persists_and_reloads(tmp_path):
    store = SessionStore(tmp_path)
    m = Memory(store, "s1")
    m.add("user", "生成 order fulfillment 的文档")
    m.add("assistant", "已完成，发现 7 列漂移")

    reloaded = Memory(SessionStore(tmp_path), "s1")
    assert [t.role for t in reloaded.turns] == ["user", "assistant"]
    assert "7 列漂移" in reloaded.turns[1].content


def test_context_includes_recent_turns(tmp_path):
    m = Memory(SessionStore(tmp_path), "s1", keep_turns=2)
    m.add("user", "第一轮")
    m.add("assistant", "第二轮")
    ctx = m.context()
    assert "最近对话" in ctx
    assert "第二轮" in ctx


def test_overflow_uses_token_budget(tmp_path):
    m = Memory(SessionStore(tmp_path), "s1", max_tokens=20)
    assert not m.overflow()
    m.add("user", "x" * 300)
    assert m.overflow()


def test_compact_reduces_turns_and_keeps_summary(tmp_path):
    m = Memory(SessionStore(tmp_path), "s1", keep_turns=1)
    for i in range(5):
        m.add("user", f"第 {i} 轮内容")

    seen: list[str] = []

    def fake_summarize(prompt: str) -> str:
        seen.append(prompt)
        return "决定：只做 schema 检查；未完成：写报告"

    summary = m.compact(fake_summarize)

    assert len(m.turns) == 1                 # 只留最近一轮
    assert "未完成" in summary
    assert seen and "第 0 轮" in seen[0]      # 旧轮次进了摘要输入

    # 摘要落盘，重开还在
    assert "未完成" in Memory(SessionStore(tmp_path), "s1").summary


def test_compact_noop_when_short(tmp_path):
    m = Memory(SessionStore(tmp_path), "s1", keep_turns=3)
    m.add("user", "only one")
    assert m.compact(lambda p: "不该被调用") == ""


def test_store_filters_bad_session_id(tmp_path):
    store = SessionStore(tmp_path)
    assert "/" not in store.path("../../etc/passwd").name


def test_secrets_are_redacted_before_persist(tmp_path):
    m = Memory(SessionStore(tmp_path), "s1")
    m.add("user", "my key is sk-abcdefghijklmnopqrstuvwx")
    raw = (tmp_path / "s1.jsonl").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwx" not in raw


def test_estimate_tokens_positive():
    assert estimate_tokens("") >= 1
    assert estimate_tokens("a" * 300) == 100


# ---------------------------------------------------------------- 长期检索
def test_recall_finds_relevant_history(tmp_path):
    store = SessionStore(tmp_path)
    Memory(store, "a").add("user", "排查 schema drift 的方法")
    Memory(store, "b").add("user", "今天天气不错")

    hits = recall(store, "schema drift")
    assert hits
    assert hits[0]["session"] == "a"
    assert hits[0]["score"] > 0


def test_recall_empty_query(tmp_path):
    store = SessionStore(tmp_path)
    Memory(store, "a").add("user", "x")
    assert recall(store, "!!!") == []


# ---------------------------------------------------------------- 重排
def test_tokens_drop_stopwords():
    got = tokens("how to detect the schema")
    assert "schema" in got
    assert "the" not in got and "how" not in got


def test_rerank_puts_exact_term_first():
    candidates = [
        {"id": "a", "heading": "Naming", "text": "naming conventions for files", "score": 0.9},
        {"id": "b", "heading": "Schema drift", "text": "detect schema drift by comparing columns", "score": 0.2},
    ]
    out = rerank("detect schema drift", candidates, top_k=2)
    assert out[0]["id"] == "b"
    assert "rerank_score" in out[0]


def test_heading_is_a_tiebreaker():
    """正文覆盖相同时，标题命中的排前面。

    注意：标题不能压过正文。正文完整命中的候选一定优于只有标题命中的候选——
    标题是加分项，不是决定项。
    """
    candidates = [
        {"id": "no_head", "heading": "Other", "text": "schema drift in body", "score": 0.5},
        {"id": "with_head", "heading": "Schema drift", "text": "schema drift in body", "score": 0.5},
    ]
    out = rerank("schema drift", candidates, top_k=2)
    assert out[0]["id"] == "with_head"

    # 正文完全不命中时，只靠标题救不回来
    body_hit = {"id": "b", "heading": "Other", "text": "schema drift fully described", "score": 0.5}
    head_only = {"id": "h", "heading": "Schema drift", "text": "nothing relevant", "score": 0.5}
    assert rerank("schema drift", [head_only, body_hit], top_k=2)[0]["id"] == "b"


def test_rerank_truncates_to_top_k():
    candidates = [
        {"id": str(i), "heading": f"h{i}", "text": f"text {i}", "score": 0.1}
        for i in range(10)
    ]
    assert len(rerank("text", candidates, top_k=3)) == 3


def test_rerank_empty():
    assert rerank("q", [], top_k=3) == []


def test_score_is_bounded():
    c = {"id": "x", "heading": "a b", "text": "a b c", "score": 0.5}
    s = score("a b c", c)
    assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# 跨会话记忆检索：四路信号
# ---------------------------------------------------------------------------
class _FakeEmbedder:
    """按关键词给固定方向，用来证明向量信号真的参与了排序。"""

    name = "fake"
    dim = 2

    def encode(self, texts):
        import numpy as np

        out = []
        for text in texts:
            if text.strip() in {"alpha", "beta"}:
                out.append([1.0, 0.0])
            elif "gamma" in text:
                out.append([0.0, 1.0])
            else:
                out.append([0.5, 0.5])
        return np.array(out, dtype=np.float32)


def test_recall_vector_breaks_a_tie(tmp_path):
    """词面完全打平的时候，向量决定顺序。

    构造：query 是 alpha，两条记忆分别是 beta 和 gamma，词面重合都是 0。
    没有向量时两者同分（只靠时间衰减），顺序等于插入顺序；
    加向量后 beta 应该排第一。这证明向量那一路真的参与了计算。
    """
    store = SessionStore(tmp_path)
    store.append("s1", Turn("user", "beta", ts=1000))
    store.append("s2", Turn("user", "gamma", ts=1000))

    without = recall(store, "alpha", top_k=2, now=1000, min_score=0)
    assert [h["content"] for h in without] == ["beta", "gamma"]

    with_vector = recall(
        store, "alpha", top_k=2, embedder=_FakeEmbedder(), now=1000, min_score=0
    )
    assert with_vector[0]["content"] == "beta"
    assert with_vector[0]["vector"] == 1.0


def test_recall_dedups_and_prefers_newer(tmp_path):
    """同一句话在两个会话里出现，只留一条，而且留新的那条。"""
    store = SessionStore(tmp_path)
    store.append("old", Turn("user", "schema drift check", ts=100))
    store.append("new", Turn("user", "schema drift check", ts=900))

    hits = recall(store, "schema drift", top_k=5, now=1000, min_score=0)
    assert len(hits) == 1
    assert hits[0]["session"] == "new"


def test_recall_decays_with_age(tmp_path):
    store = SessionStore(tmp_path)
    store.append("s", Turn("user", "schema drift check", ts=0))
    fresh = recall(store, "schema drift", now=0, min_score=0)[0]["recency"]
    stale = recall(store, "schema drift", now=30 * 86400, min_score=0)[0]["recency"]
    assert fresh > stale


def test_recall_min_score_filters_noise(tmp_path):
    store = SessionStore(tmp_path)
    store.append("s", Turn("user", "完全不相关的闲聊", ts=0))
    assert recall(store, "schema drift", now=60 * 86400, min_score=0.15) == []


def test_recall_degrades_when_embedder_raises(tmp_path):
    class Broken:
        name = "broken"
        dim = 2

        def encode(self, texts):
            raise RuntimeError("model not found")

    store = SessionStore(tmp_path)
    store.append("s", Turn("user", "schema drift check", ts=1000))
    hits = recall(store, "schema drift", embedder=Broken(), now=1000, min_score=0)
    assert hits and hits[0]["vector"] is None
