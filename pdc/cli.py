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
from types import SimpleNamespace

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


def _load_memory(session_id: str, out_dir: Path):
    """--session 非空时才启用记忆。不启用时行为完全不变。"""
    if not session_id:
        return None
    from pdc.memory import Memory, SessionStore

    store = SessionStore(out_dir / "memory")
    return Memory(store, session_id=session_id)


def _goal_with_memory(target: str, memory) -> str:
    if memory is None:
        return ""
    ctx = memory.context()
    if not ctx:
        return ""
    return (
        "以下是这个会话之前的内容，仅作背景参考，不要重复已经做过的事：\n"
        f"{ctx}\n\n"
        f"现在为 `{target}` 生成设计文档。"
    )


def _remember(memory, target: str, result) -> None:
    """写进记忆的不是原始输出，而是结构化结论。

    直接存 result.output 有两个问题：
      1. 失败信息（"达到最大步数"）会被当成结论，下一轮污染上下文
      2. 原文动辄几千字，一个会话几轮就把预算占满
    所以只存"做了什么、结果如何、发现了几个问题"。
    """
    memory.add("user", f"为 {target} 生成设计文档")

    if result.written and not result.issues:
        files = result.written.get("xlsx", "")
        memory.add(
            "assistant",
            f"已完成：产出设计文档（{files}），自检通过，未解决项 0。",
        )
    elif result.written:
        memory.add(
            "assistant",
            f"部分完成：已产出文档，但有 {len(result.issues)} 个未解决问题："
            + "；".join(result.issues[:3]),
        )
    else:
        memory.add("assistant", f"未完成：{(result.output or '无输出')[:150]}")


def _compact(memory, llm) -> None:
    """窗口溢出时压缩。失败不影响主流程。"""
    try:
        memory.compact(lambda prompt: llm.chat([{"role": "user", "content": prompt}], []).content)
    except Exception as exc:  # noqa: BLE001
        print(f"[memory] 压缩失败，保留原样: {exc}", file=sys.stderr)


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
        mcp_client = None
        if getattr(args, "mcp", ""):
            from pdc.mcp_client import MCPClient

            mcp_client = MCPClient(args.mcp.split()).start()
            print(f"[mcp] 已连接，工具: {[t['name'] for t in mcp_client.tools]}")
        try:
            return audit_main(
                str(target), model=args.model or "deepseek-chat",
                out=args.out, mcp_client=mcp_client,
            )
        finally:
            if mcp_client is not None:
                mcp_client.close()

    out_dir = Path(args.out)
    memory = _load_memory(getattr(args, "session", ""), out_dir)
    goal = _goal_with_memory(args.name, memory)
    tracer = Tracer(path=out_dir / f"{args.name.replace('.yaml', '')}-trace.jsonl")

    toolbox = Toolbox(repo=Path(args.repo) if args.repo else None, out_dir=out_dir, tracer=tracer)

    mcp_cmd = getattr(args, "mcp", "")
    mcp_client = None
    if mcp_cmd:
        from pdc.mcp_client import MCPClient

        mcp_client = MCPClient(mcp_cmd.split()).start()
        toolbox.attach_mcp(mcp_client)
        print(f"[mcp] 已连接，工具: {[t['name'] for t in mcp_client.tools]}")

    if args.mode == "offline":
        llm = RuleBasedLLM()
    else:
        if not args.model:
            print("auto 模式需要 --model，或使用 --mode offline", file=sys.stderr)
            return 2
        llm = _make_llm(args.model, getattr(args, "route", ""))

    goal = goal or (
        f"为 pipeline `{args.name}` 生成设计文档。\n"
        f"要求：\n"
        f"1. 先用工具获取事实，不要凭印象\n"
        f"2. 所有列数/差异判断必须调用 diff_schema\n"
        f"3. 生成后必须自检，有问题就修\n"
        f"4. 最终汇报文件路径和 high 级问题数量"
    )

    engine = getattr(args, "engine", "react")

    if engine == "plan":
        from pdc.planner import execute_plan

        digest = ""
        try:
            digest = toolbox.digest(args.name)
        except Exception:  # noqa: BLE001 - 取不到就退化成原行为
            digest = ""
        plan_result = execute_plan(
            llm, toolbox, goal, max_steps_per_step=4, digest=digest,
            schemas=toolbox.tool_schemas(),
        )

        result = SimpleNamespace(
            output=plan_result.final,
            steps=plan_result.total_steps,
            written={},
            issues=[],
        )
        print("[engine=plan] 步骤：")
        for st in plan_result.plan:
            print(f"  {st.index}. {st.title}（{st.steps_used} 步）")

    elif engine == "graph":
        from pdc.langgraph_engine import run_graph

        g = run_graph(llm, toolbox, goal, max_steps=args.max_steps)
        result = SimpleNamespace(
            output=g["output"] or "（图执行结束，无最终文本）",
            steps=g["steps"],
            written={},
            issues=[],
        )
        print(f"[engine=graph] 节点步数 {g['steps']}，消息 {len(g['messages'])} 条")

    elif engine == "multi":
        from pdc.multiagent import run_multi

        multi = run_multi(llm, toolbox, goal)
        result = SimpleNamespace(
            output=multi.final,
            steps=multi.total_steps,
            written={},
            issues=[],
        )
        print("[engine=multi] 调度：")
        for d in multi.delegations:
            print(f"  -> {d.worker}: {d.task[:60]}")

    else:
        result = run_with_self_check(
            llm,
            toolbox,
            goal,
            max_steps=args.max_steps,
            user_context={"pipeline": args.name, "artifact": args.name},
        )

    if memory is not None:
        _remember(memory, args.name, result)
        if memory.overflow() and args.mode != "offline":
            _compact(memory, llm)
        print(f"[memory] session={memory.session_id} turns={len(memory.turns)} "
              f"tokens≈{memory.used_tokens()} summary={'Y' if memory.summary else 'N'}")

    if mcp_client is not None:
        mcp_client.close()

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


def cmd_build(args: argparse.Namespace) -> int:
    """设计文档 -> YAML。产物由模型写，校验器判。

    为什么没有 --mode offline：
      一旦给这条路加一个"模板渲染"的离线实现，这个任务就退化成 workflow，
      也就失去了它存在的意义。离线只能跑到"读出需求"这一步（--dry-run）。
    """
    from pdc.design_reader import SpecReadError, read_design
    from pdc.trace import Tracer
    from pdc.yamlgen import requirement_from_design, requirement_from_text, run_build

    source = Path(args.source)
    if not source.exists():
        print(f"找不到输入文件: {source}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            spec = read_design(source)
        except SpecReadError as exc:
            print(f"读不了设计文档: {exc}", file=sys.stderr)
            return 2
        req = requirement_from_design(spec)
    elif source.suffix.lower() in {".md", ".txt"}:
        req = requirement_from_text(source.read_text(encoding="utf-8"), args.name)
    else:
        print("只接受 .xlsx（结构化设计文档）或 .md/.txt（自由文本需求）", file=sys.stderr)
        return 2

    print(f"[build] 输入={source.name} 形态={req.label} pipeline={req.pipeline}")
    for w in req.warnings:
        print(f"[build] 文档告警: {w}")

    if args.dry_run:
        print("\n--- 读到的需求 ---\n")
        print(req.prompt)
        return 0

    if not (os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
        print(
            "生成 YAML 必须用真实模型：产物是模型写的，没有模型就没有产物。\n"
            "设置 OPENAI_API_KEY（或 DEEPSEEK_API_KEY）后重试；只看需求用 --dry-run。",
            file=sys.stderr,
        )
        return 2

    llm = _make_llm(args.model, getattr(args, "route", ""))
    tracer = Tracer(path=out_dir / f"{req.pipeline}-build-trace.jsonl")

    result = run_build(
        llm,
        req,
        max_rounds=args.max_rounds,
        out_dir=out_dir,
        examples_dir=Path(args.examples) if args.examples else None,
        tracer=tracer,
        budget=args.budget or None,
    )

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print("\n--- trace ---")
    print(json.dumps(tracer.stats(), ensure_ascii=False, indent=2))

    if result.passed:
        print(f"\n✅ 校验通过，产物: {result.yaml_path}")
        print(f"   下一步: uv run pdc parse {result.yaml_path}")
        return 0

    print(f"\n❌ 未通过（{result.rounds} 轮）。草稿保留在 {result.yaml_path or '无'}，未交付。")
    return 1


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
    a.add_argument(
        "--mcp", default="",
        help='连接 MCP server，例如 --mcp "python -m pdc.mcp_server"',
    )
    a.add_argument(
        "--engine", choices=["react", "plan", "multi", "graph"], default="react",
        help="执行范式：react / plan / multi / graph（LangGraph）",
    )
    a.add_argument(
        "--session", default="",
        help="会话 ID。给了就启用记忆（短期窗口 + 摘要 + 落盘）",
    )
    a.set_defaults(func=cmd_agent)

    b = sub.add_parser("build", help="设计文档 -> pipeline YAML（模型写，校验器判）")
    b.add_argument("source", help=".xlsx 设计文档，或 .md/.txt 需求文本")
    b.add_argument("--name", default="", help="自由文本模式下指定 pipeline 名称")
    b.add_argument("--out", default=str(DEFAULT_OUT))
    b.add_argument("--model", default="deepseek-chat")
    b.add_argument("--max-rounds", type=int, default=8)
    b.add_argument("--budget", type=int, default=0, help="token 预算，0 表示不限")
    b.add_argument("--dry-run", action="store_true", help="只打印读到的需求，不调模型")
    b.add_argument(
        "--examples", default="",
        help="参考示例目录。评测时用它做留一法，避免模型直接抄到源文件",
    )
    b.add_argument("--route", choices=["", "cost", "quality", "balanced"], default="")
    b.set_defaults(func=cmd_build)

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
