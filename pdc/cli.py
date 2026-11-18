"""命令行入口。

    pdc parse   <yaml> [--diff]                  # M0：只解析，不调模型
    pdc design  <yaml> [--out DIR]               # 确定性构建 + 渲染（不含模型）
    pdc agent   <name> [--mode offline|auto]     # 完整 Agent 循环
    pdc eval    [--cases N]                      # 跑评测集
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pdc.agent import run_with_self_check
from pdc.checks import schema_diff
from pdc.design import build_design
from pdc.llm import OpenAICompatibleLLM, RuleBasedLLM
from pdc.parser import PipelineParseError, parse_pipeline
from pdc.render import render_review_md, render_xlsx
from pdc.tools import Toolbox
from pdc.trace import Tracer

DEFAULT_OUT = Path.cwd() / "out"


def cmd_parse(args: argparse.Namespace) -> int:
    try:
        facts = parse_pipeline(args.path)
    except PipelineParseError as exc:
        print(f"解析失败: {exc}", file=sys.stderr)
        return 2

    out = facts.summary()
    if args.diff:
        out["schema_diff"] = []
        for i, job in enumerate(facts.jobs):
            for issue in schema_diff(facts, i):
                out["schema_diff"].append({"from_job": job.job_type, "issue": issue})
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_design(args: argparse.Namespace) -> int:
    facts = parse_pipeline(args.path)
    doc = build_design(facts)
    out = Path(args.out)
    xlsx = render_xlsx(doc, out / f"{doc.pipeline}-design.xlsx")
    md = render_review_md(doc, out / f"{doc.pipeline}-review.md")
    print(json.dumps({
        "pipeline": doc.pipeline,
        "sheets": len(doc.sheets),
        "findings": len(doc.findings),
        "xlsx": str(xlsx),
        "review": str(md),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    tracer = Tracer(path=out_dir / f"{args.name.replace('.yaml', '')}-trace.jsonl")

    toolbox = Toolbox(repo=Path(args.repo) if args.repo else None, out_dir=out_dir, tracer=tracer)

    if args.mode == "offline":
        llm = RuleBasedLLM()
    else:
        if not args.model:
            print("auto 模式需要 --model，或使用 --mode offline", file=sys.stderr)
            return 2
        llm = OpenAICompatibleLLM(args.model)

    goal = (
        f"为 pipeline `{args.name}` 生成设计文档。\n"
        f"要求：\n"
        f"1. 先用工具获取事实，不要凭印象\n"
        f"2. 所有列数/差异判断必须调用 diff_schema\n"
        f"3. 生成后必须自检，有问题就修\n"
        f"4. 最终汇报文件路径和 high 级问题数量"
    )

    result = run_with_self_check(
        llm,
        toolbox,
        goal,
        max_steps=args.max_steps,
        user_context={"pipeline": args.name, "artifact": args.name},
    )

    print(result.output)
    print("\n--- trace ---")
    print(json.dumps(tracer.stats(), ensure_ascii=False, indent=2))
    if result.written:
        print("\n--- files ---")
        print(json.dumps(result.written, ensure_ascii=False, indent=2))
    if result.issues:
        print("\n--- unresolved issues ---")
        for i in result.issues:
            print(f"  - {i}")
    return 0 if not result.issues else 1


def cmd_eval(args: argparse.Namespace) -> int:
    from eval.run import main as eval_main
    from pdc.scoring import parse_weights

    return eval_main(
        limit=args.cases,
        mode=args.mode,
        out=Path(args.out),
        weights=parse_weights(args.weights),
        sabotage=args.sabotage,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pdc", description="Pipeline Design Copilot")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="解析 YAML，输出结构化事实")
    p.add_argument("path")
    p.add_argument("--diff", action="store_true")
    p.set_defaults(func=cmd_parse)

    d = sub.add_parser("design", help="确定性构建设计文档（不调模型）")
    d.add_argument("path")
    d.add_argument("--out", default=str(DEFAULT_OUT))
    d.set_defaults(func=cmd_design)

    a = sub.add_parser("agent", help="完整 Agent 循环")
    a.add_argument("name", help="pipeline 文件名")
    a.add_argument("--mode", choices=["offline", "auto"], default="offline")
    a.add_argument("--model", default="")
    a.add_argument("--repo", default="")
    a.add_argument("--out", default=str(DEFAULT_OUT))
    a.add_argument("--max-steps", type=int, default=15)
    a.set_defaults(func=cmd_agent)

    e = sub.add_parser("eval", help="跑评测集")
    e.add_argument("--cases", type=int, default=20)
    e.add_argument("--mode", choices=["offline", "auto"], default="offline")
    e.add_argument("--out", default=str(DEFAULT_OUT))
    e.add_argument("--weights", default="", help="例如 structural=0.2,grounding=0.4")
    e.add_argument("--sabotage", action="store_true", help="注入一条幻觉，验证检测器有效")
    e.set_defaults(func=cmd_eval)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
