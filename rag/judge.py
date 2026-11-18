"""用 LLM 做检索相关性初标（LLM-as-judge）。

**这不能替代人工标注。**
它的定位是：
  1. 让人工标注从 157 行降到"抽检 15 行"
  2. 提供一个可以复算、可审计的初标结果
  3. 在面试里作为"LLM 初标 + 人工校验"的工程实践来讲

关键约束：
  - judge 看不到 bm25_rank / vec_rank，否则会偏向排序靠前的候选
  - 用完整 chunk 文本，不用 160 字预览
  - 不确定一律标 0（宁缺勿滥，否则 recall 虚高）
  - 每条判定都记录理由，供人工抽检时对照
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI  # noqa: E402

from pdc.rag import RuleIndex  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "rag"

SYSTEM = """你是检索相关性标注员。给你一个问题和若干候选文档片段，判断每个片段是否包含回答问题所需的信息。

判断标准（严格）：
1. 片段必须包含能直接回答问题的内容，才标 1。
2. 只是出现相关关键词、但没有解释或结论的，标 0。
3. 目录、导航、链接列表、"参见 xxx"这类，标 0。
4. 内容只沾边但不回答问题的，标 0。
5. 不确定时标 0。宁可漏标，不要错标。

只输出 JSON，格式：
{"relevant": [1, 3], "reasons": {"1": "一句话理由", "3": "一句话理由"}}
其中 1/3 是候选的编号。没有任何相关片段时输出 {"relevant": [], "reasons": {}}。"""


def judge_one(client, model: str, query: str, candidates: list[dict]) -> dict:
    blocks = []
    for i, c in enumerate(candidates, 1):
        blocks.append(f"### 候选 {i}\n标题: {c['heading']}\n内容:\n{c['text'][:1200]}\n")
    user = f"问题：{query}\n\n" + "\n".join(blocks) + "\n请判断哪些候选能回答这个问题。"

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            rel = [int(x) for x in data.get("relevant", []) if str(x).isdigit()]
            return {
                "relevant": [i for i in rel if 1 <= i <= len(candidates)],
                "reasons": data.get("reasons", {}),
                "tokens": getattr(resp.usage, "total_tokens", 0),
            }
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                return {"relevant": [], "reasons": {"error": str(exc)}, "tokens": 0}
            time.sleep(1.5 * (attempt + 1))
    return {"relevant": [], "reasons": {}, "tokens": 0}


def main(src_name: str = "label-sheet.csv", out_stem: str = "label-sheet-llm") -> int:
    src = OUT / src_name
    dst = OUT / f"{out_stem}.csv"
    notes_path = OUT / f"{out_stem}-notes.json"

    if not src.exists():
        print(f"找不到 {src}，先跑 rag.label", file=sys.stderr)
        return 2

    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("需要 DEEPSEEK_API_KEY 或 OPENAI_API_KEY", file=sys.stderr)
        return 2

    client = OpenAI(
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com"),
        api_key=api_key,
    )
    model = os.getenv("JUDGE_MODEL", "deepseek-chat")

    index = RuleIndex()
    by_id = {c["id"]: c for c in index.chunks}

    rows = list(csv.DictReader(src.open(encoding="utf-8-sig")))
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["qid"], []).append(r)

    total_tokens = 0
    notes: dict[str, dict] = {}

    for qid, group in grouped.items():
        query = group[0]["query"]
        # 关键：只把 heading 和正文给 judge，绝不传 bm25_rank / vec_rank
        candidates = [by_id[r["chunk_id"]] for r in group]
        result = judge_one(client, model, query, candidates)
        total_tokens += result["tokens"]

        picked = set(result["relevant"])
        for i, r in enumerate(group, 1):
            r["relevant"] = "1" if i in picked else "0"

        notes[qid] = {
            "query": query,
            "relevant_indices": sorted(picked),
            "relevant_chunks": [group[i - 1]["chunk_id"] for i in sorted(picked)],
            "reasons": result["reasons"],
        }
        print(f"{qid}  {query[:44]:46} -> {sorted(picked)}")

    with dst.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")

    n_rel = sum(r["relevant"] == "1" for r in rows)
    print(f"\n标注完成: {len(rows)} 行，相关 {n_rel} 行，消耗 {total_tokens} tokens")
    print(f"输出: {dst}")
    print(f"理由: {notes_path}")
    print("\n注意：这是 LLM 初标，不是人工标注。")
    print("用于汇报前必须人工抽检至少 15 行，见 README 的「人工抽检」一节。")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="label-sheet.csv")
    ap.add_argument("--out", default="label-sheet-llm")
    a = ap.parse_args()
    raise SystemExit(main(src_name=a.labels, out_stem=a.out))
