"""评测运行器。

跑法：
    uv run pdc eval --cases 8
    uv run python -m eval.run --mode offline

输出四个指标：
    structural  —— 文档结构是否完整（sheet 数、落地表行数）
    contract    —— 校验器是否通过
    detection   —— 已知缺陷是否被检出
    stability   —— 同输入两次结果是否一致（幂等）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from pdc.design import build_design
from pdc.parser import parse_pipeline
from pdc.render import render_review_md, render_xlsx
from pdc.grounding import ground, grounding_score
from pdc.ir import Finding
from pdc.rag import RuleIndex
from pdc.scoring import Metrics, parse_weights, weighted_score
from pdc.tools import DEFAULT_REPO
from pdc.validate import validate_doc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval.cases import CASES  # noqa: E402


SABOTAGE = Finding(
    code="schema_mismatch",
    severity="high",
    title="注入的幻觉用例",
    detail="9 列在数据帧里但 schema 未声明: ['FUNDING_GEOGRAPHY_KEY', 'FAKE_INVENTED_COLUMN']",
    rule_id="99-rule-that-does-not-exist",
    evidence="/tmp/does-not-exist.yaml",
)


def run_case(case: dict, out_dir: Path, sabotage: bool = False) -> dict:
    name = case["name"]
    matches = list(DEFAULT_REPO.rglob(name))
    if not matches:
        return {"id": case["id"], "skipped": True, "reason": "文件不存在"}

    started = time.perf_counter()
    facts = parse_pipeline(matches[0])
    doc = build_design(facts)
    if sabotage and case["id"] == "spend-details":
        doc.findings.append(SABOTAGE)

    xlsx = render_xlsx(doc, out_dir / case["id"] / "design.xlsx")
    render_review_md(doc, out_dir / case["id"] / "review.md")
    elapsed = int((time.perf_counter() - started) * 1000)

    issues = validate_doc(doc, xlsx)

    rules = RuleIndex()
    violations = ground(doc, facts, rules.rule_ids)
    g_score = grounding_score(violations, len(doc.findings))
    codes = {f.code for f in doc.findings}
    landings = [s for s in doc.sheets if s.name.startswith("Landing - ")]
    targets = doc.sheet("Load Targets")
    target_rows = len(targets.rows) if targets else 0

    ok_detection = True
    if case.get("expect_finding"):
        ok_detection = case["expect_finding"] in codes
    if case.get("expect_clean"):
        ok_detection = ok_detection and "schema_mismatch" not in codes

    return {
        "id": case["id"],
        "pipeline": doc.pipeline,
        "jobs": len(facts.jobs),
        "sheets": len(doc.sheets),
        "landings": len(landings),
        "load_targets": target_rows,
        "landing_rows": sum(len(s.rows) for s in landings),
        "findings": len(doc.findings),
        "codes": sorted(codes),
        "validation_issues": issues,
        "grounding_violations": [str(v) for v in violations],
        "grounding": g_score,
        "contract_ok": not issues,
        "detection_ok": ok_detection,
        "ms": elapsed,
        # 有 schema 的 job 出 Landing 表；没有 schema 的 job 至少要出 Load Targets 行
        "structural_ok": len(doc.sheets) >= 6 and (len(landings) >= 1 or target_rows >= 1),
    }


def main(
    limit: int = 20,
    mode: str = "offline",
    out: Path | None = None,
    weights: dict[str, float] | None = None,
    sabotage: bool = False,
) -> int:
    out_dir = Path(out or Path.cwd() / "out" / "eval")
    results = []
    for case in CASES[:limit]:
        r = run_case(case, out_dir, sabotage=sabotage)
        results.append(r)
        flag = "SKIP" if r.get("skipped") else ("PASS" if _pass(r) else "FAIL")
        if r.get("grounding_violations"):
            for v in r["grounding_violations"]:
                print(f"       {v}")
        print(f"[{flag}] {r['id']:20} sheets={r.get('sheets', '-'):>2} "
              f"landings={r.get('landings', '-'):>2} findings={r.get('findings', '-'):>2} "
              f"grounding={r.get('grounding', '-'):>5} {r.get('ms', '-')}ms")

    ran = [r for r in results if not r.get("skipped")]
    n = max(len(ran), 1)

    # 幂等性：同名 case 跑两次，结构必须一致
    stability = 1.0
    dup = [c for c in CASES[:limit] if c.get("idempotent_with")]
    if dup:
        by_id = {r["id"]: r for r in ran}
        for c in dup:
            a = by_id.get(c["idempotent_with"])
            b = by_id.get(c["id"])
            if a and b:
                same = (a["sheets"], a["landing_rows"], tuple(a["codes"])) == (
                    b["sheets"], b["landing_rows"], tuple(b["codes"])
                )
                stability = 1.0 if same else 0.0

    m = Metrics(
        structural=round(sum(1 for r in ran if r["structural_ok"]) / n, 3),
        contract=round(sum(1 for r in ran if r["contract_ok"]) / n, 3),
        detection=round(sum(1 for r in ran if r["detection_ok"]) / n, 3),
        grounding=round(sum(r["grounding"] for r in ran) / n, 3),
        stability=stability,
        extra={
            "cases": len(ran),
            "avg_ms": round(sum(r["ms"] for r in ran) / n),
            "total_findings": sum(r["findings"] for r in ran),
            "grounding_violations": sum(len(r["grounding_violations"]) for r in ran),
        },
    )
    w = weights or parse_weights(None)
    metrics = {**m.as_dict(), "weights": w, "weighted": weighted_score(m, w)}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps({"metrics": metrics, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n--- metrics ---")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"\nreport: {out_dir / 'report.json'}")
    return 0 if metrics["weighted"] >= 0.9 else 1


def _pass(r: dict) -> bool:
    """四条硬门槛。grounding 不达标就视为失败 —— 宁可误报，不可放过。"""
    return bool(
        r.get("structural_ok")
        and r.get("contract_ok")
        and r.get("detection_ok")
        and r.get("grounding", 1.0) >= 0.999
    )


if __name__ == "__main__":
    raise SystemExit(main())
