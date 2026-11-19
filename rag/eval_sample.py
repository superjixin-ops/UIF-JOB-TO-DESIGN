"""开源样例语料上的检索评测（34 个 chunk，14 条人工标注 query）。

为什么单独做一个：

     仓库里原来引用的 R@5 / α 曲线来自内部语料（370 个 chunk），
     那份语料没有随仓库发布，所以那些数字在本仓库**无法复现**。

     这个脚本用 `samples/rules` + `samples/skills` 重新标了一遍，
     所有数字都能用一条命令跑出来。

     它不是"更大规模实验"的替代品——34 个 chunk、14 条 query，
     只能说明机制在真实文本上有效，不能说明泛化。

跑法：
    uv run python -m rag.eval_sample

标注方式：
    先读完全部 chunk，再写 query；query 用"用户会怎么问"的措辞，
    不抄原文关键词。gold 是能回答这个问题的 chunk。
    标注只写一次，不根据检索结果回头修改——否则就是拿答案调参。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.embed import get_embedder
from pdc.rag import RuleIndex
from pdc.rerank import rerank
from rag.eval import mrr, rank_bm25, rank_hybrid, rank_vector, recall_at_k

# (query, 能回答它的 chunk id)
CASES: list[tuple[str, str]] = [
    ("流水线跑完了但是下游一行数据都没有，从哪查", "01-pipeline-structure#Common failure conditions"),
    ("一个 job 里能不能有多个主数据集", "01-pipeline-structure#Extract rules"),
    ("目标表少了几列但任务不报错，怎么提前发现", "02-load-schema#Schema drift"),
    ("小数类型的列需要声明精度吗", "02-load-schema#The column contract"),
    ("为什么团队不推荐直接用 SQL 写转换", "03-transform-patterns#Prefer individual transforms over SparkSql"),
    ("join 之后行数比预期少了很多", "03-transform-patterns#Joins"),
    ("怎么给一个 job 选集群规格", "04-sizing#The tiers"),
    ("加了资源还是很慢，问题可能出在哪", "04-sizing#What size does not fix"),
    ("评审一份设计文档应该从哪些方面看", "05-review-checklist#How to review a design document"),
    ("多个来源写同一张维度表，最后一个把前面的覆盖了", "skill-dimension-pattern#The problem"),
    ("评审意见怎么写才算清楚可执行", "05-review-checklist#How to write findings"),
    ("先做过滤还是先做 join", "03-transform-patterns#Filters"),
    ("声明了不可为空但数据里出现空值", "02-load-schema#Nullability pitfalls"),
    ("怎么估算一个任务的成本", "04-sizing#Sizing and cost"),
]


def evaluate() -> dict:
    index = RuleIndex()
    chunks = index.chunks
    ids = [c["id"] for c in chunks]
    known = set(ids)

    missing = [g for _, g in CASES if g not in known]
    if missing:
        raise SystemExit(f"标注里的 chunk id 不存在: {missing}")

    labels = {q: {g} for q, g in CASES}
    queries = {q: q for q, _ in CASES}

    # --- 三路召回 ---
    bm25_rank = {q: rank_bm25(index, q) for q in queries}
    embedder = get_embedder()
    vectors = embedder.encode([c["text"] for c in chunks])
    vec_rank = {q: rank_vector(chunks, vectors, embedder, q) for q in queries}

    results: dict[str, dict] = {}
    for alpha in (0.1, 0.3, 0.5):
        results[f"hybrid α={alpha}"] = {
            q: rank_hybrid(bm25_rank[q], vec_rank[q], alpha) for q in queries
        }
    results["vector only"] = vec_rank
    results["bm25 only"] = bm25_rank

    # --- 重排：拿 hybrid α=0.1 的 top-10 当候选池 ---
    by_id = {c["id"]: c for c in chunks}
    reranked: dict[str, list[str]] = {}
    for q in queries:
        pool = [by_id[cid] for cid in results["hybrid α=0.1"][q][:10]]
        reranked[q] = [c["id"] for c in rerank(q, pool, top_k=len(pool))]
    results["hybrid + rerank"] = reranked

    rows = {}
    for name, ranked in results.items():
        rows[name] = {
            "r@1": recall_at_k(ranked, labels, 1),
            "r@3": recall_at_k(ranked, labels, 3),
            "r@5": recall_at_k(ranked, labels, 5),
            "mrr": mrr(ranked, labels),
        }
    return {"chunks": len(chunks), "queries": len(CASES), "rows": rows}


def main() -> int:
    out = evaluate()
    print(f"语料 {out['chunks']} 个 chunk，{out['queries']} 条人工标注 query\n")
    print(f"{'方法':<20}{'R@1':>8}{'R@3':>8}{'R@5':>8}{'MRR':>8}")
    print("-" * 52)
    for name, row in out["rows"].items():
        print(f"{name:<20}{row['r@1']:>8.3f}{row['r@3']:>8.3f}{row['r@5']:>8.3f}{row['mrr']:>8.3f}")
    print("\n注意：34 个 chunk、14 条 query，只能说明机制有效，不能说明泛化。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
