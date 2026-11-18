"""审计模式评测：它到底能不能把已知问题都找出来。

和文档生成的 eval 不同，这里评的是**观点**，不是结构：
    golden 是一组"人工确认存在的问题"，看审计报告命中了几个。

跑法（需要模型）：
    uv run python -m eval.audit_eval

不设 key 会跳过，避免 CI 里意外花钱。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.audit import run_audit  # noqa: E402
from pdc.paths import repo_dir  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "audit-eval"

# 每条的 keyword 用来判断"命中了没有"：
# 不要求措辞一致，只要求报告里出现了这些关键标识。
GOLDEN = [
    {"pipeline": "fact-order-fulfillment", "keyword": "7 列", "why": "schema 漂移 7 列"},
    {"pipeline": "fact-order-fulfillment", "keyword": "schema", "why": "self-serve 无 schema"},
    {"pipeline": "fact-inventory-snapshot", "keyword": "main_df", "why": "两个 main_df"},
]

# 不该报出来的（误报检查）
NOT_PROBLEMS = [
    {"pipeline": "dim-product-family", "why": "这条流水线本身是干净的"},
]


def main() -> int:
    if not (os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
        print("跳过：没有设置 API key")
        return 0

    from pdc.llm import OpenAICompatibleLLM

    scope = repo_dir()
    print(f"审计目录: {scope}")

    result = run_audit(OpenAICompatibleLLM("deepseek-chat"), scope, out_dir=OUT / "run")

    blob = "\n".join(
        f"{f['title']} {f['detail']}" for f in result.findings + result.rejected
    ).lower()

    hits = []
    for case in GOLDEN:
        ok = case["keyword"].lower() in blob
        hits.append(ok)
        print(f"  [{'✓' if ok else '✗'}] {case['pipeline']}: {case['why']}")

    detected = sum(hits)
    rate = detected / len(GOLDEN)

    false_pos = [
        f for f in result.findings
        if any(np["pipeline"] in f["title"].lower() for np in NOT_PROBLEMS)
        and f["severity"] == "high"
    ]

    print()
    print(f"检出率: {detected}/{len(GOLDEN)} = {rate:.2f}")
    print(f"检查文件: {len(result.inspected)}   步数: {result.steps}")
    print(f"报告发现: {len(result.findings)}   自检驳回: {len(result.rejected)}")
    print(f"误报（干净文件被判 high）: {len(false_pos)}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps({
        "detection_rate": rate,
        "detected": detected,
        "total": len(GOLDEN),
        "inspected": result.inspected,
        "steps": result.steps,
        "findings": result.findings,
        "rejected": result.rejected,
        "false_positives": false_pos,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0 if rate >= 0.66 else 1


if __name__ == "__main__":
    raise SystemExit(main())
