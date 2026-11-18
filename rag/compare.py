"""对比不同嵌入模型的检索效果。

为什么必须做这一步：
    标注是 chunk 级别的，与嵌入模型无关。
    所以同一批标签可以评估任意检索器 —— 这才是公平对比。

    如果只用 bge-small-zh 跑英文语料就下结论"向量不行"，
    那是把模型选型问题误判成方法问题。

用法：
    HF_ENDPOINT=https://hf-mirror.com uv run python -m rag.compare
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from pdc.embed import FastEmbedder  # noqa: E402
from pdc.rag import RuleIndex  # noqa: E402
from rag.eval import load_labels, mrr, rank_hybrid, recall_at_k  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "rag"

MODELS = [
    "BAAI/bge-small-zh-v1.5",
    "BAAI/bge-small-en-v1.5",
]


def main() -> int:
    labels_path = OUT / "label-sheet-final.csv"
    if not labels_path.exists():
        print(f"找不到 {labels_path}", file=sys.stderr)
        return 2

    queries, labels = load_labels(labels_path)
    active = [q for q in queries if q in labels]
    print(f"query={len(active)}  知识库=370  相关段落={sum(len(v) for v in labels.values())}\n")

    index = RuleIndex()
    texts = [c["text"] for c in index.chunks]
    ids = [c["id"] for c in index.chunks]

    bm25_rank = {q: [h["id"] for h in index.search(queries[q], top_k=len(index.chunks))] for q in active}

    rows = [{
        "retriever": "bm25",
        "R@1": recall_at_k(bm25_rank, labels, 1),
        "R@3": recall_at_k(bm25_rank, labels, 3),
        "R@5": recall_at_k(bm25_rank, labels, 5),
        "MRR": mrr(bm25_rank, labels),
    }]

    for model in MODELS:
        try:
            emb = FastEmbedder(model)
            vecs = emb.encode(texts)
        except Exception as exc:  # noqa: BLE001
            print(f"跳过 {model}: {exc}")
            continue

        vec_rank = {}
        for q in active:
            qv = emb.encode([queries[q]])[0]
            sims = vecs @ qv
            vec_rank[q] = [ids[i] for i in np.argsort(-sims)]

        short = model.split("/")[-1]
        rows.append({
            "retriever": f"vector[{short}]",
            "R@1": recall_at_k(vec_rank, labels, 1),
            "R@3": recall_at_k(vec_rank, labels, 3),
            "R@5": recall_at_k(vec_rank, labels, 5),
            "MRR": mrr(vec_rank, labels),
        })

        best = None
        for alpha in [round(x * 0.1, 1) for x in range(11)]:
            hy = {q: rank_hybrid(bm25_rank[q], vec_rank[q], alpha) for q in active}
            row = {
                "retriever": f"hybrid[{short}](a={alpha:.1f})",
                "R@1": recall_at_k(hy, labels, 1),
                "R@3": recall_at_k(hy, labels, 3),
                "R@5": recall_at_k(hy, labels, 5),
                "MRR": mrr(hy, labels),
                "alpha": alpha,
            }
            rows.append(row)
            if best is None or row["R@5"] > best["R@5"]:
                best = row
        print(f"{short}: 最佳 α={best['alpha']}  R@5={best['R@5']}  MRR={best['MRR']}")

    print(f"\n{'retriever':<34} {'R@1':>6} {'R@3':>6} {'R@5':>6} {'MRR':>6}")
    print("-" * 64)
    for r in rows:
        if "hybrid" in r["retriever"] and r.get("R@5", 0) < 0.6:
            continue  # 只打印有竞争力的 α，避免刷屏
        print(f"{r['retriever']:<34} {r['R@1']:>6.3f} {r['R@3']:>6.3f} {r['R@5']:>6.3f} {r['MRR']:>6.3f}")

    (OUT / "compare.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {OUT / 'compare.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
