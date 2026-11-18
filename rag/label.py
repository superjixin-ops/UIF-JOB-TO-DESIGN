"""生成人工标注表。

关键方法论：**候选池必须是各检索器的并集。**

如果只给 BM25 的 top-k，那么向量检索命中的段落根本不在池子里，
标注完永远算不出向量赢在哪 —— 这叫选择性偏差，会让弱的那一方被高估。

所以这里：
  candidates = BM25_top5 ∪ Vector_top5

每行让你勾 1/0，最后算 recall@k。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from pdc.embed import get_embedder  # noqa: E402
from pdc.rag import RuleIndex  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "rag"

# 18 条真实会问的问题。混合了三类：
#   术语类  —— 关键词能命中，BM25 的强项
#   概念类  —— 表述与文档不同，向量才可能赢
#   跨文档  —— 需要综合多处，两者都难
QUERIES = [
    # 中文提问 + 夹英文术语：BM25 靠术语命中，向量靠语义
    ("q01", "FilterType2 的 includeDeleted 参数是什么意思", "中文术语"),
    ("q02", "when must a fact pipeline declare its load schema", "英文概念"),
    ("q03", "pipeline 的 size 应该怎么选，T 还是 M", "中文概念"),
    ("q04", "when to use ForeignKey instead of extracting dimensions", "英文概念"),
    ("q05", "SparkSql 和单个 transform 应该选哪个", "中文概念"),
    ("q06", "AddSurrogateKey 的 naturalKey 和 deltaColumns 有什么区别", "中文术语"),
    ("q07", "MCP 是什么，解决什么问题", "中文概念"),
    ("q08", "naming conventions for pipeline files", "英文概念"),
    ("q09", "how to load a dimension that has multiple source systems", "英文跨文档"),
    ("q10", "overwrite 和 append 分别在什么场景用", "中文概念"),
    ("q11", "what should be checked in a design review", "英文跨文档"),
    ("q12", "DAG 的任务依赖是怎么定义的", "中文概念"),
    ("q13", "how to troubleshoot a pipeline that runs slowly", "英文跨文档"),
    ("q14", "UT 是什么，和维度有什么关系", "中文术语"),
    ("q15", "how to distinguish dev and prod domains", "英文概念"),
    ("q16", "listener 是做什么的", "中文概念"),
    ("q17", "how does the template mechanism work", "英文概念"),
    ("q18", "credentials 和 connector 有什么区别", "中文概念"),
]


def build_pool(index: RuleIndex, embedder, top_k: int = 5) -> dict[str, list[dict]]:
    texts = [c["text"] for c in index.chunks]
    vectors = embedder.encode(texts)

    pool: dict[str, list[dict]] = {}
    for qid, query, _kind in QUERIES:
        bm25_hits = index.search(query, top_k=top_k)

        qv = embedder.encode([query])[0]
        sims = vectors @ qv
        order = np.argsort(-sims)[:top_k]
        vector_hits = [
            {**index.chunks[i], "score": float(sims[i])} for i in order
        ]

        merged: dict[str, dict] = {}
        for rank, hit in enumerate(bm25_hits, 1):
            merged.setdefault(hit["id"], {**hit, "bm25_rank": rank, "vec_rank": ""})
        for rank, hit in enumerate(vector_hits, 1):
            if hit["id"] in merged:
                merged[hit["id"]]["vec_rank"] = rank
            else:
                merged[hit["id"]] = {**hit, "bm25_rank": "", "vec_rank": rank}

        pool[qid] = list(merged.values())

    return pool


def main(top_k: int = 5, out_path: str | None = None) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    index = RuleIndex()
    embedder = get_embedder()
    print(f"chunks={len(index.chunks)}  embedder={embedder.name}  top_k={top_k}")

    pool = build_pool(index, embedder, top_k=top_k)

    rows = []
    for qid, query, kind in QUERIES:
        for i, cand in enumerate(pool[qid], 1):
            rows.append({
                "qid": qid,
                "query": query,
                "kind": kind,
                "cand": i,
                "chunk_id": cand["id"],
                "bm25_rank": cand["bm25_rank"],
                "vec_rank": cand["vec_rank"],
                "heading": cand["heading"],
                "preview": cand["text"].replace("\n", " ")[:160],
                "relevant": "",
            })

    stem = Path(out_path).stem if out_path else "label-sheet"
    csv_path = OUT / f"{stem}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    md_path = OUT / f"{stem}.md"
    lines = [
        "# 检索标注表",
        "",
        "对每一条：**如果这段内容能回答问题，就在 `relevant` 填 1，否则填 0**。",
        "不要看 `bm25_rank` / `vec_rank` 猜答案 —— 只看 preview 和 heading 判断。",
        "",
        "填完保存 CSV，然后运行：`uv run python -m rag.eval`",
        "",
    ]
    for qid, query, kind in QUERIES:
        lines.append(f"## {qid} · [{kind}] {query}")
        lines.append("")
        lines.append("| # | chunk | 内容预览 | relevant |")
        lines.append("|---|---|---|---|")
        for i, c in enumerate(pool[qid], 1):
            prev = c["text"].replace("\n", " ")[:110].replace("|", "\\|")
            lines.append(f"| {i} | `{c['heading'][:50]}` | {prev} | |")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"标注表: {csv_path}")
    print(f"可读版: {md_path}")
    print(f"共 {len(rows)} 行，{len(QUERIES)} 条 query")

    # 顺便报告候选池的构成，确认并集策略生效
    only_vec = sum(1 for r in rows if r["bm25_rank"] == "" and r["vec_rank"] != "")
    only_bm25 = sum(1 for r in rows if r["vec_rank"] == "" and r["bm25_rank"] != "")
    print(f"仅向量命中: {only_vec} 行   仅 BM25 命中: {only_bm25} 行   （并集策略生效）")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5, help="每个检索器取多少条")
    ap.add_argument("--out", default=None, help="输出文件名前缀")
    a = ap.parse_args()
    raise SystemExit(main(top_k=a.top_k, out_path=a.out))
