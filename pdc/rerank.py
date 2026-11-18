"""重排（Rerank）：召回之后、送进 prompt 之前的最后一道排序。

为什么需要重排：
    召回阶段（BM25 / 向量）追求"别漏"，所以宽进；
    重排阶段追求"把最对的排前面"，所以严出。
    两级分工是检索系统的标准结构。

为什么不直接上 cross-encoder：
    那要再加载一个模型（100MB+），而这批语料只有 5 篇、每条 query 只召回 5 条。
    用词法重排先把结构搭对，等语料上量再换模型——这本身就是取舍。

词法重排的三个信号：
    1. 词覆盖率      query 的词有多少出现在候选里
    2. 标题命中      词出现在标题里比出现在正文里更重要
    3. 邻近度        多个词在文中相邻出现，通常意味着同一句话在讲这件事
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]")

# 停用词会稀释覆盖率：query 里的 "how / to / the" 在每篇文档里都有。
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "how", "what", "why", "when", "which", "who", "to", "of", "for",
    "and", "or", "in", "on", "at", "by", "with", "from", "as", "it",
    "do", "does", "did", "should", "can", "could", "would", "will",
    "i", "we", "you", "my", "our", "your", "this", "that", "these",
}


def tokens(text: str, *, drop_stopwords: bool = True) -> set[str]:
    words = {t.lower() for t in _WORD.findall(text)}
    return words - STOPWORDS if drop_stopwords else words


def _coverage(query: set[str], doc: set[str]) -> float:
    if not query:
        return 0.0
    return len(query & doc) / len(query)


def _proximity(query: list[str], text: str, window: int = 60) -> float:
    """有多少个 query 词在 text 的同一窗口内共现。"""
    if len(query) < 2:
        return 0.0
    low = text.lower()
    hits = 0
    for i in range(len(query) - 1):
        a, b = query[i], query[i + 1]
        for m in re.finditer(re.escape(a), low):
            seg = low[m.start(): m.start() + window]
            if b in seg:
                hits += 1
                break
    return hits / max(len(query) - 1, 1)


def score(query: str, candidate: dict, *, heading_weight: float = 0.35,
          proximity_weight: float = 0.25) -> float:
    """给一个候选打分。返回 0~1。"""
    q = tokens(query)
    body = tokens(candidate.get("text", ""))
    head = tokens(candidate.get("heading", ""))

    cov = _coverage(q, body)
    head_hit = _coverage(q, head)
    prox = _proximity([t for t in _WORD.findall(query.lower())], candidate.get("text", ""))

    raw = (1 - heading_weight - proximity_weight) * cov \
        + heading_weight * head_hit \
        + proximity_weight * prox
    # 保留原始召回分的一点点权重，避免重排完全无视检索器
    original = float(candidate.get("score", 0.0))
    return round(0.85 * raw + 0.15 * min(original, 1.0), 4)


def rerank(query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
    """对召回结果重排。不改原对象，返回带 rerank_score 的新列表。"""
    if not candidates:
        return []
    scored = [{**c, "rerank_score": score(query, c)} for c in candidates]
    scored.sort(key=lambda c: -c["rerank_score"])
    return scored[:top_k]
