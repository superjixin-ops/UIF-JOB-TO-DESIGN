"""build 方向的评测：文档 -> YAML，留一法（leave-one-out）。

为什么必须留一法：

第一次跑评测，4 条样例全部满分，而且产物和源文件**逐字节相同**。
原因不是模型强，是参考示例目录里就有它的源文件——
模型把参考答案抄了一遍。这是评测污染（data leakage），不是能力。

改成留一法：生成 A 的时候，示例目录里只有 B/C/D。
这样"抄"这条路被堵死，剩下的分数才是真的。

四个指标：

    pass_rate    校验器通过率（唯一重要的指标：不通过就是不可交付）
    rounds       平均修复轮数（反映校验器反馈质量）
    tokens       平均 token（成本）
    exact_match  与源文件逐字节相同的比例（**必须为 0**，不为 0 说明还在抄）

另外记录 document_loss：源文件里有、但设计文档里没有的字段数。
它衡量的是"文档信息完整度"，不是模型能力——
把模型的失败和文档的缺陷混在一起，是评测最容易犯的错。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.design import build_design  # noqa: E402
from pdc.design_reader import read_design  # noqa: E402
from pdc.llm import build_llm  # noqa: E402
from pdc.parser import parse_pipeline  # noqa: E402
from pdc.render import render_xlsx  # noqa: E402
from pdc.yamlgen import requirement_from_design, run_build  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "pipelines"
OUT = ROOT / "out" / "build-eval"


def _examples_for(holdout: Path, workdir: Path) -> Path:
    """造一个不含 holdout 的示例目录。用软链，不复制。"""
    d = workdir / f"examples-{holdout.stem}"
    d.mkdir(parents=True, exist_ok=True)
    for p in SAMPLES.glob("*.yaml"):
        if p.name == holdout.name:
            continue
        link = d / p.name
        if not link.exists():
            link.symlink_to(p)
    return d


def _document_loss(original: Path, spec) -> int:
    """源文件里有、文档里没捕获的连接级字段数。

    这是文档的问题，不是模型的问题。分开记，才不会误判模型。
    """
    facts = parse_pipeline(original)
    captured = {(e.job, e.alias) for e in spec.extracts}
    loss = 0
    for job in facts.jobs:
        for e in job.extracts:
            if (job.job_type, e.alias) not in captured:
                loss += 1
    return loss


def run(cases: int = 20, model: str = "deepseek-chat") -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    samples = sorted(SAMPLES.glob("*.yaml"))[:cases]
    llm = build_llm("auto", model)

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for src in samples:
            t0 = time.time()
            facts = parse_pipeline(src)
            xlsx = render_xlsx(build_design(facts), tmpdir / f"{facts.name}.xlsx")
            spec = read_design(xlsx)
            req = requirement_from_design(spec)

            result = run_build(
                llm,
                req,
                examples_dir=_examples_for(src, tmpdir),
                out_dir=OUT / facts.name,
                max_rounds=8,
            )

            produced = Path(result.yaml_path)
            exact = (
                produced.exists()
                and produced.read_text(encoding="utf-8").strip()
                == src.read_text(encoding="utf-8").strip()
            )
            row = {
                "case": facts.name,
                "passed": result.passed,
                "rounds": result.rounds,
                "tokens": result.tokens,
                "grounding": round(result.grounding, 4),
                "stuck_events": result.stuck_events,
                "exact_match": exact,
                "document_loss": _document_loss(src, spec),
                "seconds": round(time.time() - t0, 1),
                "issues": [i["code"] for i in result.issues],
                "violations": [v["code"] for v in result.violations],
            }
            rows.append(row)
            mark = "PASS" if row["passed"] else "FAIL"
            print(
                f"[{mark}] {row['case']:26} rounds={row['rounds']:2} "
                f"tokens={row['tokens']:6} grounding={row['grounding']:.3f} "
                f"exact={row['exact_match']} loss={row['document_loss']} "
                f"{row['seconds']:5.1f}s"
            )

    n = len(rows) or 1
    summary = {
        "cases": len(rows),
        "pass_rate": round(sum(r["passed"] for r in rows) / n, 4),
        "avg_rounds": round(sum(r["rounds"] for r in rows) / n, 2),
        "avg_tokens": round(sum(r["tokens"] for r in rows) / n),
        "avg_grounding": round(sum(r["grounding"] for r in rows) / n, 4),
        "exact_match_rate": round(sum(r["exact_match"] for r in rows) / n, 4),
        "document_loss": sum(r["document_loss"] for r in rows),
        "stuck_events": sum(r["stuck_events"] for r in rows),
    }
    report = {"summary": summary, "cases": rows}
    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n--- metrics ---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nreport: {OUT / 'report.json'}")
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=20)
    ap.add_argument("--model", default="deepseek-chat")
    a = ap.parse_args()
    run(cases=a.cases, model=a.model)
