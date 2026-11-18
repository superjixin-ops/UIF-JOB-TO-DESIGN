"""命令行入口。

    pdc parse   <yaml> [--diff]                  # M0：只解析，不调模型
    pdc design  <yaml> [--out DIR]               # 确定性构建 + 渲染（不含模型）
    pdc agent   <name> [--mode offline|auto]     # 完整 Agent 循环
    pdc eval    [--cases N]                      # 跑评测集
"""

from __future__ import annotations

import argparse
import json
import os
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


def _make_llm(model: str, route: str):
    """route 非空且目录里有多个模型时，启用路由 + 熔断。"""
    from pdc.routing import RoutedLLM, Router, load_catalog

    catalog = load_catalog(os.getenv("PDC_MODELS"))
    if route and len(catalog) > 1:
        return RoutedLLM(
            Router(catalog, prefer=route),
            lambda name: OpenAICompatibleLLM(name),
        )
    return OpenAICompatibleLLM(model)


def cmd_agent(args: argparse.Namespace) -> int:
    """一个入口，两种输入。

    给文件 → 生成设计文档（目标固定）
    给目录 → 审计（目标开放，模型自己决定看什么）

    这不是两个模式，是同一个 agent 面对不同输入时的两种目标。
    """
    target = Path(args.name)

    if target.is_dir():
        from pdc.audit import main as audit_main

        # 目录审计无法用 SOP 实现，所以不看 --mode，只看有没有模型可用
        if not (os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
            print(
                "目录审计需要真实模型：它要自己决定看哪些文件，写不出固定流程。\n"
                "请先设置 OPENAI_API_KEY（或 DEEPSEEK_API_KEY）。",
                file=sys.stderr,
            )
            return 2
        return audit_main(str(target), model=args.model or "deepseek-chat", out=args.out)

    out_dir = Path(args.out)
    tracer = Tracer(path=out_dir / f"{args.name.replace('.yaml', '')}-trace.jsonl")

    toolbox = Toolbox(repo=Path(args.repo) if args.repo else None, out_dir=out_dir, tracer=tracer)

    if args.mode == "offline":
        llm = RuleBasedLLM()
    else:
        if not args.model:
            print("auto 模式需要 --model，或使用 --mode offline", file=sys.stderr)
            return 2
        llm = _make_llm(args.model, getattr(args, "route", ""))

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


def cmd_audit(args: argparse.Namespace) -> int:
    from pdc.audit import main as audit_main

    return audit_main(args.scope, model=args.model, out=args.out)


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

    a = sub.add_parser("run", help="一个入口：给文件生成文档，给目录做审计")
    a.add_argument("name", help="pipeline 文件名，或要审计的目录")
    a.add_argument("--mode", choices=["offline", "auto"], default="offline")
    a.add_argument("--model", default="deepseek-chat")
    a.add_argument("--repo", default="")
    a.add_argument("--out", default=str(DEFAULT_OUT))
    a.add_argument("--max-steps", type=int, default=15)
    a.add_argument(
        "--route", choices=["", "cost", "quality", "balanced"], default="",
        help="启用模型路由与熔断（需 PDC_MODELS 里有多于一个模型）",
    )
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
