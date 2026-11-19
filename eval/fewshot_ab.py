"""few-shot 选择策略的下游 A/B：随机 vs 结构近邻 vs 不给示例。

背景（两步实验的结论）：

    1. 结构卡片区分度：job 链 + 连接器 + 变换类型 已经能唯一识别 71% 的 pipeline
    2. 留一法最近邻：中位相似度 0.93，81.7% 同 job 链
       —— 但结构相似 ≠ 业务相似，所以只能靠下游指标判断有没有用

这个脚本只回答一个问题：**给的示例更"像"，模型会不会更快写对？**

三组：

    random  从池里随机抽 3 条
    struct  查表：链 + 连接器 + 变换 -> 链 + 连接器 -> 链，取前 3
    none    不给示例

用法：

    OPENAI_API_KEY=... uv run python -m eval.fewshot_ab --cases 12
    OPENAI_API_KEY=... uv run python -m eval.fewshot_ab --cases 1 --arms none  # 冒烟

注意：这是拿真实语料跑，只读源文件，产物写到 out/fewshot-ab/。
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.design import build_design
from pdc.design_reader import read_design
from pdc.llm import build_llm
from pdc.parser import parse_pipeline
from pdc.render import render_xlsx
from pdc.yamlgen import requirement_from_design, run_build

OUT = Path(__file__).resolve().parents[1] / "out" / "fewshot-ab"


# ---------------------------------------------------------------------------
# 池与结构相似度
# ---------------------------------------------------------------------------
def load_pool(root: Path, max_lines: int = 900) -> list[dict]:
    """收集"可用作示例"的 pipeline：有 schema、有 transform、不太大。"""
    pool = []
    for p in sorted(root.rglob("*.yaml")):
        try:
            if len(p.read_text(encoding="utf-8").splitlines()) > max_lines:
                continue
            f = parse_pipeline(p)
        except Exception:  # noqa: BLE001, S112 - 坏文件直接跳过，统计口径里单独计数
            continue
        if not (any(j.load and j.load.schema for j in f.jobs) and any(j.transforms for j in f.jobs)):
            continue
        pool.append(
            {
                "path": p,
                "name": f.name,
                "chain": tuple(j.job_type for j in f.jobs),
                "ex": tuple(sorted({e.connector for j in f.jobs for e in j.extracts})),
                "tx": tuple(sorted({t.transform_type for j in f.jobs for t in j.transforms})),
                "ncols": sum(len(j.load.schema) for j in f.jobs if j.load),
            }
        )
    return pool


def _jac(a, b) -> float:
    return len(set(a) & set(b)) / len(set(a) | set(b)) if (set(a) | set(b)) else 1.0


def similarity(a: dict, b: dict) -> float:
    """权重必须加起来等于 1，否则绝对分数没有意义。

    第一版漏了一项，最大值只有 0.75，报告里写"相似度 0.93"就再也不成立了。
    排序不受影响（等比例缩放），但人读的数字是错的——这种 bug 最难发现。

    权重依据来自语料体检：变换类型把唯一卡片率从 31% 拉到 71%，
    所以它比连接器更重要。
    """
    s = 0.40 * (a["chain"] == b["chain"])
    s += 0.20 * _jac(a["ex"], b["ex"])
    s += 0.30 * _jac(a["tx"], b["tx"])
    s += 0.10 * (1 - min(abs(a["ncols"] - b["ncols"]) / max(a["ncols"], b["ncols"], 1), 1))
    # 业务相似度不参与：没有可信来源，结构信号已经够用
    return s


def structural_neighbors(q: dict, pool: list[dict], k: int = 3) -> list[dict]:
    """三级查表：先精确结构，再放宽变换，最后只按链。"""
    levels = [
        lambda x: (x["chain"], x["ex"], x["tx"]),
        lambda x: (x["chain"], x["ex"]),
        lambda x: x["chain"],
    ]
    for keyfn in levels:
        key = keyfn(q)
        peers = [c for c in pool if c is not q and keyfn(c) == key]
        if peers:
            peers.sort(key=lambda c: -similarity(q, c))
            return peers[:k]
    # 兜底：全局最相似
    return sorted((c for c in pool if c is not q), key=lambda c: -similarity(q, c))[:k]


def random_neighbors(q: dict, pool: list[dict], k: int = 3) -> list[dict]:
    return [c for c in random.sample(pool, k) if c is not q][:k]


# ---------------------------------------------------------------------------
# 每组实验的示例目录
# ---------------------------------------------------------------------------
def prepare_examples(picked: list[dict], target: Path) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    names = []
    for i, c in enumerate(picked):
        safe = f"{i}-{c['name'][:60]}.yaml"
        shutil.copyfile(c["path"], target / safe)
        names.append(safe)
    return names


def run_arm(arm: str, spec, llm, examples_dir: Path, out_dir: Path) -> dict:
    t0 = time.time()
    result = run_build(
        llm,
        requirement_from_design(spec),
        examples_dir=examples_dir,
        out_dir=out_dir,
        max_rounds=6,
        budget=250_000,
    )
    return {
        "arm": arm,
        "passed": result.passed,
        "rounds": result.rounds,
        "tokens": result.tokens,
        "seconds": round(time.time() - t0, 1),
        "issues": [i["code"] for i in result.issues][:3],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/Users/jixinlai/Documents/CRD-7/pipelines")
    ap.add_argument("--cases", type=int, default=12)
    ap.add_argument("--arms", default="random,struct,none")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--model", default="deepseek-chat")
    args = ap.parse_args(argv)

    root = Path(args.repo)
    if not root.exists():
        print(f"找不到语料: {root}", file=sys.stderr)
        return 2

    print("[1/3] 扫描语料…")
    pool = load_pool(root)
    print(f"      可用作示例: {len(pool)} 条")

    random.seed(args.seed)
    # 用例也要限制规模，否则单次 run 很贵
    cases = [c for c in pool if c["ncols"] <= 120]
    cases = random.sample(cases, min(args.cases, len(cases)))
    print(f"      本次用例: {len(cases)} 条")

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    llm = build_llm("auto", args.model)
    OUT.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for i, case in enumerate(cases, 1):
            print(f"\n[{i}/{len(cases)}] {case['name']}  ({len(case['chain'])} jobs, {case['ncols']} cols)")
            facts = parse_pipeline(case["path"])
            xlsx = render_xlsx(build_design(facts), tmpdir / f"{case['name']}.xlsx")
            spec = read_design(xlsx)
            out_dir = OUT / case["name"]

            for arm in arms:
                ex_dir = tmpdir / f"ex-{case['name']}-{arm}"
                if ex_dir.exists():
                    shutil.rmtree(ex_dir)
                if arm == "none":
                    ex_dir.mkdir(parents=True)
                elif arm == "random":
                    prepare_examples(random_neighbors(case, pool), ex_dir)
                elif arm == "struct":
                    prepare_examples(structural_neighbors(case, pool), ex_dir)
                else:
                    raise SystemExit(f"未知 arm: {arm}")

                row = run_arm(arm, spec, llm, ex_dir, out_dir / arm)
                row["case"] = case["name"]
                rows.append(row)
                print(
                    f"    {arm:6} {'PASS' if row['passed'] else 'FAIL'}  "
                    f"rounds={row['rounds']}  tokens={row['tokens']:,}  {row['seconds']}s"
                )

    print("\n--- 汇总 ---")
    summary = {}
    for arm in arms:
        sub = [r for r in rows if r["arm"] == arm]
        if not sub:
            continue
        rounds = [r["rounds"] for r in sub]
        toks = [r["tokens"] for r in sub]
        summary[arm] = {
            "n": len(sub),
            "pass_rate": round(sum(r["passed"] for r in sub) / len(sub), 3),
            "rounds_mean": round(statistics.mean(rounds), 2),
            "rounds_std": round(statistics.stdev(rounds), 2) if len(rounds) > 1 else 0.0,
            "tokens_mean": round(statistics.mean(toks)),
            "tokens_std": round(statistics.stdev(toks)) if len(toks) > 1 else 0,
        }
        print(json.dumps({arm: summary[arm]}, ensure_ascii=False, indent=2))

    report = {"cases": len(cases), "summary": summary, "rows": rows}
    path = OUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
