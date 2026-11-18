"""检索评测：BM25 vs 向量 vs 混合，并扫 α。

用法：
    # 真实评测（先填好 label-sheet.csv 的 relevant 列）
    uv run python -m rag.eval

    # 只验证流程能跑通（自动造标签，结论不可用）
    uv run python -m rag.eval --demo

指标定义：
    recall@k —— top-k 里至少命中一条相关段落的 query 占比
    只统计 k <= 候选池深度，否则会低估（池外的相关段落我们不知道）

为什么只到 k=5：
    标注池是 BM25_top5 ∪ Vector_top5。k>5 时可能有相关段落根本没进池子，
    算出来的 recall 会偏低，误导结论。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from pdc.embed import get_embedder  # noqa: E402
from pdc.rag import RuleIndex, tokenize  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "rag"
RRF_K = 60


# ------------------------------------------------------------------ 检索器
def rank_bm25(index: RuleIndex, query: str) -> list[str]:
    hits = index.search(query, top_k=len(index.chunks))
    return [h["id"] for h in hits]


def rank_vector(chunks: list[dict], vectors: np.ndarray, embedder, query: str) -> list[str]:
    qv = embedder.encode([query])[0]
    sims = vectors @ qv
    order = np.argsort(-sims)
    return [chunks[i]["id"] for i in order]


def rank_hybrid(
    bm25_order: list[str],
    vec_order: list[str],
    alpha: float,
    *,
    rrf: bool = False,
) -> list[str]:
    """alpha 越大越偏 BM25。"""
    if rrf:
        scores: dict[str, float] = defaultdict(float)
        for rank, cid in enumerate(bm25_order, 1):
            scores[cid] += alpha / (RRF_K + rank)
        for rank, cid in enumerate(vec_order, 1):
            scores[cid] += (1 - alpha) / (RRF_K + rank)
        return sorted(scores, key=lambda x: -scores[x])

    def norm(order: list[str]) -> dict[str, float]:
        n = max(len(order), 1)
        return {cid: 1.0 - i / n for i, cid in enumerate(order)}

    b, v = norm(bm25_order), norm(vec_order)
    all_ids = set(b) | set(v)
    combined = {cid: alpha * b.get(cid, 0.0) + (1 - alpha) * v.get(cid, 0.0) for cid in all_ids}
    return sorted(combined, key=lambda x: -combined[x])


# ------------------------------------------------------------------ 指标
def recall_at_k(ranked: dict[str, list[str]], labels: dict[str, set[str]], k: int) -> float:
    if not labels:
        return 0.0
    hit = sum(1 for qid, rel in labels.items() if rel & set(ranked.get(qid, [])[:k]))
    return round(hit / len(labels), 4)


def mrr(ranked: dict[str, list[str]], labels: dict[str, set[str]]) -> float:
    total = 0.0
    for qid, rel in labels.items():
        for rank, cid in enumerate(ranked.get(qid, []), 1):
            if cid in rel:
                total += 1 / rank
                break
    return round(total / max(len(labels), 1), 4)


# ------------------------------------------------------------------ 数据
def load_labels(path: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    queries: dict[str, str] = {}
    labels: dict[str, set[str]] = defaultdict(set)
    with path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            queries[row["qid"]] = row["query"]
            if str(row.get("relevant", "")).strip() == "1":
                labels[row["qid"]].add(row["chunk_id"])
    return queries, labels


def self_test() -> bool:
    """用构造语料验证指标算法本身是否正确。

    不产出任何"结论"——只回答一个问题：这套评测代码算得对不对。
    真实结论必须来自人工标注。
    """
    corpus = [
        ("d1", "FilterType2 includeDeleted parameter controls deleted rows"),
        ("d2", "AddSurrogateKey naturalKey defines the business key"),
        ("d3", "size T S M L selects cluster resources"),
        ("d4", "listener triggers pipeline on cos object events"),
        ("d5", "MCP standardises model to tool integration"),
        ("d6", "load schema must be declared explicitly for facts"),
    ]
    chunks = [{"id": i, "text": t, "heading": i, "source": "synthetic"} for i, t in corpus]

    ranked = {
        "t1": ["d1", "d2", "d3"],
        "t2": ["d3", "d1", "d6"],
        "t3": ["d6", "d5", "d3"],
    }
    labels = {"t1": {"d1"}, "t2": {"d6"}, "t3": {"d3"}}

    ok = True

    def check(name, got, want):
        nonlocal ok
        if got != want:
            print(f"  FAIL {name}: got {got}, want {want}")
            ok = False

    check("recall@1", recall_at_k(ranked, labels, 1), 0.3333)
    check("recall@3", recall_at_k(ranked, labels, 3), 1.0)
    # t1 命中 rank1 => 1.0; t2 命中 rank3 => 0.333; t3 命中 rank3 => 0.333
    # (1.0 + 0.3333 + 0.3333) / 3 = 0.5556
    check("mrr", mrr(ranked, labels), 0.5556)

    # hybrid 排序：alpha=1 应完全等价于 BM25 顺序
    bm = ["d1", "d2", "d3", "d4", "d5", "d6"]
    vec = ["d4", "d5", "d6", "d1", "d2", "d3"]
    check("hybrid a=1 == bm25", rank_hybrid(bm, vec, 1.0), bm)
    check("hybrid a=0 == vector", rank_hybrid(bm, vec, 0.0), vec)

    # RRF 也应该保序
    check("rrf a=1 == bm25", rank_hybrid(bm, vec, 1.0, rrf=True), bm)

    print(f"  语料 {len(chunks)} 条，query 3 条")
    print("  自检" + ("通过" if ok else "失败"))
    return ok


# ------------------------------------------------------------------ 主流程
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检索评测")
    ap.add_argument("--labels", default=str(OUT / "label-sheet.csv"))
    ap.add_argument("--self-test", action="store_true", help="用构造语料验证指标算法")
    ap.add_argument("--report", default=str(OUT / "eval.json"))
    args = ap.parse_args(argv)

    if args.self_test:
        print("=== 指标算法自检（不产出任何结论）===")
        return 0 if self_test() else 1

    path = Path(args.labels)
    if not path.exists():
        print(f"找不到标注表: {path}\n先运行: uv run python -m rag.label", file=sys.stderr)
        return 2

    index = RuleIndex()
    embedder = get_embedder()
    texts = [c["text"] for c in index.chunks]
    vectors = embedder.encode(texts)

    queries, labels = load_labels(path)

    if not labels:
        print(
            "标注表里没有任何 relevant=1 的行。\n"
            f"请打开 {path} 把 relevant 列填成 1/0。\n"
            "只想验证代码是否正常，用 --self-test。",
            file=sys.stderr,
        )
        return 2

    active = [q for q in queries if q in labels]
    print(f"query={len(active)}  标注相关段落={sum(len(v) for v in labels.values())}  知识库={len(index.chunks)}")

    bm25_rank = {q: rank_bm25(index, queries[q]) for q in active}
    vec_rank = {q: rank_vector(index.chunks, vectors, embedder, queries[q]) for q in active}

    results = []

    def record(name: str, ranked: dict[str, list[str]], **extra) -> None:
        row = {
            "retriever": name,
            "recall@1": recall_at_k(ranked, labels, 1),
            "recall@3": recall_at_k(ranked, labels, 3),
            "recall@5": recall_at_k(ranked, labels, 5),
            "mrr": mrr(ranked, labels),
            **extra,
        }
        results.append(row)

    record("bm25", bm25_rank)
    record("vector", vec_rank)

    best = None
    for alpha in [round(x * 0.1, 1) for x in range(11)]:
        hybrid = {
            q: rank_hybrid(bm25_rank[q], vec_rank[q], alpha) for q in active
        }
        row = {
            "retriever": f"hybrid(a={alpha:.1f})",
            "recall@1": recall_at_k(hybrid, labels, 1),
            "recall@3": recall_at_k(hybrid, labels, 3),
            "recall@5": recall_at_k(hybrid, labels, 5),
            "mrr": mrr(hybrid, labels),
            "alpha": alpha,
        }
        results.append(row)
        if best is None or (row["recall@5"], row["mrr"]) > (best["recall@5"], best["mrr"]):
            best = row

    for rrf_alpha in (0.3, 0.5, 0.7):
        hybrid = {
            q: rank_hybrid(bm25_rank[q], vec_rank[q], rrf_alpha, rrf=True) for q in active
        }
        results.append({
            "retriever": f"rrf(a={rrf_alpha:.1f})",
            "recall@1": recall_at_k(hybrid, labels, 1),
            "recall@3": recall_at_k(hybrid, labels, 3),
            "recall@5": recall_at_k(hybrid, labels, 5),
            "mrr": mrr(hybrid, labels),
            "alpha": rrf_alpha,
        })

    print(f"\n{'retriever':<18} {'R@1':>6} {'R@3':>6} {'R@5':>6} {'MRR':>6}")
    print("-" * 50)
    for r in results:
        print(f"{r['retriever']:<18} {r['recall@1']:>6.3f} {r['recall@3']:>6.3f} {r['recall@5']:>6.3f} {r['mrr']:>6.3f}")

    print(f"\n最佳: {best['retriever']}  R@5={best['recall@5']}  MRR={best['mrr']}")

    report = {
        "queries": len(active),
        "corpus": len(index.chunks),
        "embedder": embedder.name,
        "best": best,
        "results": results,
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
