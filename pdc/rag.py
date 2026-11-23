"""BM25 规则检索。不依赖向量库，因为规则库只有 14 篇。

为什么先上 BM25：
  - 规则里有大量专有名词（FilterType2、includeDeleted、t_sizes），关键词精确匹配比语义更准
  - 零外部依赖，可离线、可单测、可解释（能说出命中了哪个词）
什么时候再上向量：查询是"为什么这里要 dedupe"这类没有字面对应的问题时。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from pdc.paths import rules_dir as _rules_dir
from pdc.paths import skills_dir as _skills_dir

if TYPE_CHECKING:
    import numpy as np

    from pdc.embed import Embedder

DEFAULT_RULES = _rules_dir()
DEFAULT_SKILLS = _skills_dir()

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """英文按词、中文按字。规则库是中英混排，中文按字足够（术语大多是英文）。"""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


class RuleIndex:
    def __init__(self, rules_dir: Path | None = None, skills_dir: Path | None = None) -> None:
        self.rules_dir = Path(rules_dir or DEFAULT_RULES)
        # skills 也是知识库的一部分：多源维度、DDL、增量加载这些模式只写在 skill 里
        self.skills_dir = Path(skills_dir or DEFAULT_SKILLS)
        self.chunks: list[dict] = []
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._avg_len = 0.0
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self.rules_dir.exists():
            for md in sorted(self.rules_dir.glob("*.md")):
                self._index_file(md, md.stem)
        if self.skills_dir.exists():
            # 有的大写 SKILL.md，有的小写 skill.md，都要收
            skill_files = sorted(
                list(self.skills_dir.glob("*/SKILL.md")) + list(self.skills_dir.glob("*/skill.md"))
            )
            for md in skill_files:
                # 用目录名做前缀，避免 10 个 skill 都叫 "SKILL" 撞 id
                self._index_file(md, f"skill-{md.parent.name}")
        total = sum(sum(c.values()) for c in self._tf)
        self._avg_len = total / max(len(self._tf), 1)

    def _index_file(self, md: Path, key: str) -> None:
        text = md.read_text(encoding="utf-8", errors="ignore")
        for block in re.split(r"\n(?=##+ )", text):
            if len(block.strip()) < 60:
                continue
            heading = block.strip().splitlines()[0].lstrip("# ").strip()[:90]
            tokens = tokenize(block)
            if not tokens:
                continue
            self.chunks.append(
                {"id": f"{key}#{heading}", "heading": heading,
                 "source": str(md), "text": block.strip()[:1500]}
            )
            self._tf.append(Counter(tokens))
            self._df.update(set(tokens))

    @property
    def rule_ids(self) -> set[str]:
        """规则文档的 id 集合（文件名去扩展名），用于引用回查。"""
        return {c["id"].split("#")[0] for c in self.chunks}

    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 5, k1: float = 1.5, b: float = 0.75) -> list[dict]:
        if not self.chunks:
            return []
        q = tokenize(query)
        n = len(self.chunks)
        scores: list[float] = []

        for tf in self._tf:
            dl = sum(tf.values()) or 1
            score = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                f = tf[term]
                score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / self._avg_len))
            scores.append(score)

        order = sorted(range(n), key=lambda i: -scores[i])[:top_k]
        return [
            {**self.chunks[i], "score": round(scores[i], 4),
             "matched_terms": sorted({t for t in q if t in self._tf[i]})}
            for i in order
            if scores[i] > 0
        ]


# ---------------------------------------------------------------------------
# 混合检索：接进 Agent 的 search_rules 工具
# ---------------------------------------------------------------------------
class HybridRetriever:
    """BM25 + 向量，α 控制 BM25 的权重。

    α=0.1 来自实测（见 README「评测结果」）：
      R@5 0.667 -> 0.933，MRR 从 0.604 略降到 0.638。
    选 α 时按优化目标定，不是拍脑袋。

    降级策略：嵌入模型加载失败（无网络 / 无缓存）时自动退回纯 BM25，
    并在返回结果里带 degraded=True。Agent 不会因此失败，但可观测。
    """

    def __init__(
        self,
        index: RuleIndex | None = None,
        *,
        alpha: float = 0.1,
        model: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.index = index or RuleIndex()
        self.alpha = alpha
        self.model_name = model or "BAAI/bge-small-en-v1.5"
        self.enabled = enabled
        # 注意区分两件事：
        #   disabled —— 主动关掉（PDC_RAG_MODE=bm25），是正常配置
        #   degraded —— 想用向量但加载失败，是异常
        # 之前把两者混成一个字段，导致 A/B 测试里 bm25 组被误报为降级。
        self.disabled = not enabled
        self.degraded = False
        self._embedder: Embedder | None = None
        self._vectors: np.ndarray | None = None
        self._ids: list[str] = []

    @property
    def rule_ids(self) -> set[str]:
        return self.index.rule_ids

    @property
    def chunks(self) -> list[dict]:
        return self.index.chunks

    # ------------------------------------------------------------------
    def _ensure_vectors(self) -> bool:
        if self._vectors is not None:
            return True
        if not self.enabled:
            return False
        try:
            from pdc.embed import FastEmbedder

            embedder = FastEmbedder(self.model_name)
            self._embedder = embedder
            texts = [c["text"] for c in self.index.chunks]
            self._vectors = embedder.encode(texts)
            self._ids = [c["id"] for c in self.index.chunks]
            return True
        except Exception:  # noqa: BLE001 - 任何原因失败都降级，不能让 Agent 崩
            self.degraded = True
            self._vectors = None
            return False

    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 5, rerank: bool | None = None) -> list[dict]:
        """召回 → 可选重排。

        rerank 默认读环境变量 PDC_RERANK（默认开）。
        重排候选池比最终 top_k 大一倍，否则重排没有腾挪空间。
        """
        if rerank is None:
            import os

            rerank = os.getenv("PDC_RERANK", "1") != "0"

        pool = max(top_k * 2, top_k)
        bm25_hits = self.index.search(query, top_k=len(self.index.chunks) or 1)

        if not self._ensure_vectors():
            return _maybe_rerank(query, bm25_hits[:pool], top_k, rerank)

        import numpy as np

        assert self._embedder is not None
        qv = self._embedder.encode([query])[0]
        sims = self._vectors @ qv
        order = np.argsort(-sims)

        # 归一化到 [0,1] 再线性加权，避免两路分数尺度不同
        n = max(len(bm25_hits), 1)
        bm25_score = {h["id"]: 1.0 - i / n for i, h in enumerate(bm25_hits)}
        vec_score = {self._ids[i]: 1.0 - rank / n for rank, i in enumerate(order)}

        by_id = {c["id"]: c for c in self.index.chunks}
        merged: list[dict] = []
        for cid in set(bm25_score) | set(vec_score):
            score = self.alpha * bm25_score.get(cid, 0.0) + (1 - self.alpha) * vec_score.get(cid, 0.0)
            chunk = by_id[cid]
            merged.append({**chunk, "score": round(float(score), 4), "degraded": False})

        merged.sort(key=lambda x: -x["score"])
        return _maybe_rerank(query, merged[:pool], top_k, rerank)


def _maybe_rerank(query: str, hits: list[dict], top_k: int, enabled: bool) -> list[dict]:
    if not enabled or len(hits) <= 1:
        return hits[:top_k]
    from pdc.rerank import rerank as do_rerank

    return do_rerank(query, hits, top_k=top_k)


_INDEX_CACHE: dict[str, RuleIndex] = {}


def get_retriever(alpha: float | None = None, enabled: bool | None = None) -> HybridRetriever:
    """进程内复用索引和向量，避免每次 new Toolbox 都重算。"""
    import os

    if alpha is None:
        alpha = float(os.getenv("PDC_RAG_ALPHA", "0.1"))
    if enabled is None:
        enabled = os.getenv("PDC_RAG_MODE", "hybrid").lower() != "bm25"

    key = "default"
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = RuleIndex()

    retriever = HybridRetriever(_INDEX_CACHE[key], alpha=alpha, enabled=enabled)
    if enabled:
        retriever._ensure_vectors()  # 预热，让首次 search 不慢
    return retriever
