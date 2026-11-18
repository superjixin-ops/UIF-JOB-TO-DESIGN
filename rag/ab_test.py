"""A/B 测试：BM25 vs Hybrid，各跑 N 次，出均值 ± 标准差。

为什么要重复：
    单次运行的引用数受 LLM 随机性影响极大（同输入可能 1 条也可能 3 条）。
    n=1 的差异不能当结论，n=5 的均值才有讨论价值。

指标设计：
    - citations        模型最终引用了几条规则
    - grounded         其中几条是本次真的检索到过的
    - any_citation     这次运行有没有引用（二值，比均值更稳健）
    - tokens_in        输入 token（检索质量好 → 上下文更短 → 更便宜）

诚实的边界：
    n=5 仍然很小，做不了严格显著性检验。报告均值±标准差 + 全部原始值，
    让读者自己判断，而不是给一个 p 值假装严谨。
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.agent import run_with_self_check  # noqa: E402
from pdc.citations import RuleCitations  # noqa: E402
from pdc.llm import OpenAICompatibleLLM  # noqa: E402
from pdc.tools import Toolbox  # noqa: E402
from pdc.trace import Tracer  # noqa: E402
from rag.analyze import read_run, report  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "ab"
PIPELINE = "fact-order-fulfillment.yaml"
GOAL = (
    f"为 pipeline `{PIPELINE}` 生成设计文档。\n"
    "1. 先用工具获取事实\n2. 列数/差异必须调用 diff_schema\n"
    "3. 生成后自检，有问题就修\n4. 汇报文件路径和 high 级问题数"
)


def one_run(mode: str, i: int) -> dict:
    os.environ["PDC_RAG_MODE"] = mode
    run_dir = OUT / mode / f"run{i}"
    run_dir.mkdir(parents=True, exist_ok=True)

    tracer = Tracer(path=run_dir / "trace.jsonl")
    toolbox = Toolbox(out_dir=run_dir, tracer=tracer)
    llm = OpenAICompatibleLLM("deepseek-chat")

    started = time.perf_counter()
    result = run_with_self_check(
        llm, toolbox, GOAL,
        max_steps=12,
        user_context={"pipeline": PIPELINE, "artifact": PIPELINE},
    )
    elapsed = int((time.perf_counter() - started) * 1000)

    run = read_run(run_dir / "trace.jsonl")
    metrics = report(run, RuleCitations())
    stats = tracer.stats()

    return {
        "mode": mode,
        "run": i,
        "degraded": bool(getattr(toolbox.rules, "degraded", False)),
        "ok": not result.issues,
        "citations": metrics["citations"],
        "grounded": metrics["citations_retrieved"],
        "retrieved_chunks": metrics["retrieved_chunks"],
        "search_calls": metrics["search_calls"],
        "tokens_in": stats["tokens_in"],
        "tokens_out": stats["tokens_out"],
        "elapsed_ms": elapsed,
    }


def summarize(rows: list[dict]) -> dict:
    def stat(key: str) -> tuple[float, float]:
        vals = [r[key] for r in rows]
        return (
            round(statistics.mean(vals), 2),
            round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0,
        )

    mean_c, std_c = stat("citations")
    mean_g, std_g = stat("grounded")
    mean_t, std_t = stat("tokens_in")
    return {
        "n": len(rows),
        "citations_mean": mean_c,
        "citations_std": std_c,
        "grounded_mean": mean_g,
        "grounded_std": std_g,
        "any_citation_rate": round(sum(1 for r in rows if r["citations"] > 0) / len(rows), 2),
        "tokens_in_mean": mean_t,
        "tokens_in_std": std_t,
        "all_ok": all(r["ok"] for r in rows),
        "degraded_runs": sum(1 for r in rows if r["degraded"]),
    }


def main(n: int = 5) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    for mode in ("bm25", "hybrid"):
        for i in range(1, n + 1):
            r = one_run(mode, i)
            rows.append(r)
            print(
                f"{mode:7} run{i}  citations={r['citations']} grounded={r['grounded']} "
                f"chunks={r['retrieved_chunks']:2} tok={r['tokens_in']:6} "
                f"{r['elapsed_ms']/1000:.1f}s  degraded={r['degraded']}"
            )

    summary = {"bm25": summarize([r for r in rows if r["mode"] == "bm25"]),
               "hybrid": summarize([r for r in rows if r["mode"] == "hybrid"])}

    print(f"\n{'指标':<22} {'BM25':>14} {'Hybrid':>14}")
    print("-" * 52)
    for key, label in [
        ("citations_mean", "引用规则数 mean±std"),
        ("grounded_mean", "其中被检索到 mean±std"),
        ("any_citation_rate", "至少引用一条的比例"),
        ("tokens_in_mean", "输入 token mean±std"),
    ]:
        a, b = summary["bm25"], summary["hybrid"]
        if key.endswith("_mean") and f"{key[:-5]}_std" in a:
            print(f"{label:<22} {a[key]:>7.2f}±{a[key[:-5]+'_std']:<5.2f} {b[key]:>7.2f}±{b[key[:-5]+'_std']:<5.2f}")
        else:
            print(f"{label:<22} {a[key]:>14} {b[key]:>14}")

    if summary["bm25"]["degraded_runs"] or summary["hybrid"]["degraded_runs"]:
        print("\n警告：有运行发生检索降级，结果不可信")

    (OUT / "ab-results.json").write_text(
        json.dumps({"summary": summary, "runs": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n明细: {OUT / 'ab-results.json'}")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="每个 arm 跑几次")
    args = ap.parse_args()
    raise SystemExit(main(args.n))
