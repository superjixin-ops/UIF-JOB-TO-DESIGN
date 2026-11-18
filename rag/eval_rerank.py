"""重排效果实测（样例语料，6 条 query）。

为什么单独做这个小评测：
    仓库里没有现成的相关性标注，而"感觉重排更好"不是结论。
    6 条 query 覆盖样例规则的 5 篇文档，人工指定 gold，够验证机制。
    语料上量后换成交互式标注，接口不变。

跑法：
    uv run python -m rag.eval_rerank
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.rag import RuleIndex  # noqa: E402
from pdc.rerank import rerank  # noqa: E402

# (query, gold chunk id)
CASES = [
    ("how to detect schema drift", "02-load-schema#Schema drift"),
    ("which size for a daily incremental fact", "04-sizing#The tiers"),
    ("why avoid big SparkSql blocks", "03-transform-patterns#Prefer individual transforms over SparkSql"),
    ("what to check before starting a review", "05-review-checklist#Structural checks"),
    ("how to handle nullable columns before load", "02-load-schema#Nullability pitfalls"),
    ("does a bigger size fix data skew", "04-sizing#What size does not fix"),
]


def recall_at(order: list[str], gold: str, k: int) -> float:
    return 1.0 if gold in order[:k] else 0.0


def main() -> int:
    index = RuleIndex()
    print(f"语料: {len(index.chunks)} chunks  query: {len(CASES)}\n")

    base = {"R@1": 0.0, "R@3": 0.0}
    reranked = {"R@1": 0.0, "R@3": 0.0}

    print(f"{'query':46} {'gold':38} {'bm25':>6} {'+rerank':>8}")
    print("-" * 104)

    for query, gold in CASES:
        # 召回 6 条（比最终 top3 宽），再分别看两种排序
        hits = index.search(query, top_k=len(index.chunks))
        bm25_order = [h["id"] for h in hits]

        pool = hits[:6]
        rr_order = [h["id"] for h in rerank(query, pool, top_k=6)]

        r1_b = recall_at(bm25_order, gold, 1)
        r3_b = recall_at(bm25_order, gold, 3)
        r1_r = recall_at(rr_order, gold, 1)
        r3_r = recall_at(rr_order, gold, 3)

        base["R@1"] += r1_b
        base["R@3"] += r3_b
        reranked["R@1"] += r1_r
        reranked["R@3"] += r3_r

        rank_b = bm25_order.index(gold) + 1 if gold in bm25_order else "-"
        rank_r = rr_order.index(gold) + 1 if gold in rr_order else "-"
        print(f"{query[:44]:46} {gold.split('#')[-1][:36]:38} {str(rank_b):>6} {str(rank_r):>8}")

    n = len(CASES)
    print("\n" + "=" * 52)
    print(f"{'指标':10} {'BM25':>10} {'+rerank':>10} {'变化':>8}")
    print("-" * 52)
    for k in ("R@1", "R@3"):
        b, r = base[k] / n, reranked[k] / n
        delta = r - b
        print(f"{k:10} {b:>10.3f} {r:>10.3f} {delta:>+8.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
