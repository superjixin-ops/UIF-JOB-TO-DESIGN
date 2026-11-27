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

from pdc.agent import AgentResult, run_with_self_check
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

    from pdc.memory import recall

    parts: list[str] = []
    ctx = memory.context()
    if ctx:
        parts.append(ctx)

    # 跨会话召回：单线程 CLI，可以放心加载向量模型。
    # API 里没这么做——多 worker 各加载一份模型不划算，见 docs/memory-design.md。
    hits: list[dict] = []
    try:
        from pdc.embed import get_embedder

        hits = recall(memory.store, target, top_k=4, embedder=get_embedder(),
                      same_session=memory.session_id)
    except Exception:  # noqa: BLE001 - 向量不可用就退化成词面
        hits = recall(memory.store, target, top_k=4, same_session=memory.session_id)

    other = [h for h in hits if h["session"] != memory.session_id]
    if other:
        lines = [f"- [{h['session']}] {h['content'][:200]}" for h in other]
        parts.append("[跨会话相关记忆]\n" + "\n".join(lines))
        print(f"[memory] 召回 {len(other)} 条跨会话记忆")

    if not parts:
        return ""
    return (
        "以下是历史内容，仅作背景参考，不要重复已经做过的事：\n"
        + "\n\n".join(parts)
        + f"\n\n现在为 `{target}` 生成设计文档。"
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

        result: AgentResult | SimpleNamespace = SimpleNamespace(
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

    elif engine == "langchain":
        from pdc.langchain_engine import run_langchain

        lc = run_langchain(
            toolbox, goal, model=args.model or "deepseek-chat", max_steps=args.max_steps
        )
        result = SimpleNamespace(
            output=lc["output"],
            steps=lc["steps"],
            written={},
            issues=[],
        )
        stats = toolbox.tracer.stats()
        print(
            f"[engine=langchain] 工具调用 {lc['tool_calls']} 次，消息 {lc['steps']} 条，"
            f"llm {stats.get('llm_calls', 0)} 次 / {stats.get('tokens_in', 0) + stats.get('tokens_out', 0):,} tok"
        )

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
    """设计文档 -> YAML。

    两条路，按输入形态自动选：

        认识的模板家族   确定性计划器生成（pdc/plan.py），不调模型
        不认识           模型写，校验器判（pdc/yamlgen.py）

    为什么不是"全部交给模型"：
        模板家族内的顺序和参数是编译，不是翻译。模型每轮都会漏掉
        不同的十步，而漏掉的那一步校验器看不出来（参数为空也合法）。
        为什么不是"全部确定性"：
        文档里的长段落（打分规则、哈希口径）必须理解，那是模型的工作。
        所以是"确定性优先，模型兜底"，不是二选一。
    """
    from pdc.design_reader import SpecReadError, read_design
    from pdc.parser import parse_pipeline
    from pdc.plan import compile_spec
    from pdc.spec_validator import validate_text
    from pdc.trace import Tracer
    from pdc.yamlgen import requirement_from_design, requirement_from_text, run_build

    source = Path(args.source)
    if not source.exists():
        print(f"找不到输入文件: {source}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 读文档时可能要用模型翻译长段落的变换说明，所以先把模型建好。
    # 没有 key 也能读，只是长段落会被跳过（--dry-run 用得上）。
    llm = None
    if os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY"):
        llm = _make_llm(args.model, getattr(args, "route", ""))

    spec = None
    if source.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            spec = read_design(source, llm=llm)
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

    # ---------- 确定性路径 ----------
    compiled = (
        compile_spec(spec)
        if (spec is not None and args.engine != "model")
        else None
    )
    if compiled is not None:
        text, plan_stats = compiled
        issues = [i for i in validate_text(text) if i.severity == "error"]
        if issues:
            # 确定性产物过不了自己的校验器，是编译器的 bug，不是输入的问题。
            # 不退回模型——那会把 bug 藏起来，让它变成"偶发质量波动"。
            print("❌ 计划器产物未通过结构校验（这是代码缺陷，不是文档问题）：", file=sys.stderr)
            for i in issues:
                print(f"  [{i.code}] {i.message}", file=sys.stderr)
            return 1

        target = out_dir / f"{req.pipeline}.yaml"
        target.write_text(text, encoding="utf-8")

        # 参数级校验：结构合法不等于参数合法。
        # 这里拦的是"少了必填参数"这种运行时才会炸的错。
        catalog_issues = plan_stats.get("catalog_issues") or []
        if catalog_issues:
            print(json.dumps({
                "status": "failed",
                **plan_stats,
                "catalog_violations": catalog_issues,
            }, ensure_ascii=False, indent=2))
            print(f"\n❌ 产物未通过字典校验（编译器 bug），保留在 {target}", file=sys.stderr)
            return 1

        facts = parse_pipeline(target)
        violations = req.ground(facts)
        blocking = [v for v in violations if v.severity == "error"]
        grounding = max(0.0, 1.0 - len(blocking) / max(req.item_count, 1))
        print(json.dumps({
            "status": "passed" if not blocking else "failed",
            **plan_stats,
            "grounding": round(grounding, 4),
            "violations": [v.to_dict() for v in blocking],
        }, ensure_ascii=False, indent=2))
        if blocking:
            print(f"\n❌ 弱契约未通过，产物保留在 {target}", file=sys.stderr)
            return 1
        print(f"\n✅ 校验通过，产物: {target}")
        print(f"   下一步: uv run pdc parse {target}")
        return 0

    # ---------- 模型路径 ----------
    if llm is None:
        print(
            "这份文档不属于已知模板家族，生成 YAML 需要模型。\n"
            "设置 OPENAI_API_KEY（或 DEEPSEEK_API_KEY）后重试；只看需求用 --dry-run。",
            file=sys.stderr,
        )
        return 2
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


def cmd_index(args: argparse.Namespace) -> int:
    from pdc.search import build, update

    if args.action == "update":
        result = update(
            args.repo,
            args.out,
            backend=args.backend,
            offline_embed=args.offline,
            limit=args.limit or None,
            dsn=args.dsn,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(
            f"\n新增 {result['added']} / 变更 {result['changed']} / 删除 {result['removed']}，"
            f"只 embed 了 {result['embedded']} 条，耗时 {result.get('seconds', 0)}s"
        )
        return 0

    meta = build(
        args.repo,
        args.out,
        backend=args.backend,
        offline_embed=args.offline,
        limit=args.limit or None,
        dsn=args.dsn,
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"\n下一步: uv run pdc search \"订单履约\" --index {args.out}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from pdc.search import search

    where = {}
    for item in args.where or []:
        if "=" in item:
            k, v = item.split("=", 1)
            where[k] = v
    hits = search(
        args.index,
        args.query,
        k=args.top_k,
        where=where or None,
        offline_embed=args.offline,
        dsn=args.dsn,
    )
    if not hits:
        print("没有命中。")
        return 0
    for i, h in enumerate(hits, 1):
        print(f"{i}. {h.id}  (score {h.score:.3f})")
        print(f"   {h.metadata.get('path', '')}")
        if h.metadata.get("chain"):
            print(f"   {h.metadata['chain']}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from pdc.search import bench

    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]
    out = bench(args.index, sizes, queries=args.queries)
    print(f"{'n':>8}{'numpy 建':>10}{'sqlite 建':>11}{'numpy p50':>12}{'sqlite p50':>12}{'db MB':>8}")
    for r in out["rows"]:
        print(
            f"{r['n']:>8}{r['numpy_build_s']:>9}s{r['sqlite_build_s']:>10}s"
            f"{r['numpy_p50_ms']:>11}ms{r['sqlite_p50_ms']:>11}ms{r['db_size_mb']:>8}"
        )
    print(f"\n每条查询取 {out['queries']} 次的中位数。")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    from pdc.memory import SessionStore, recall

    store = SessionStore(Path(args.dir))
    if args.action == "list":
        print(json.dumps({"sessions": store.list_sessions()}, ensure_ascii=False, indent=2))
        return 0

    embedder = None
    if not args.no_vector:
        try:
            from pdc.embed import get_embedder

            embedder = get_embedder()
        except Exception:  # noqa: BLE001
            print("[memory] 向量不可用，退化为词面检索", file=sys.stderr)

    hits = recall(store, args.query, top_k=args.top, embedder=embedder)
    if not hits:
        print("没有命中。")
        return 0
    for i, h in enumerate(hits, 1):
        print(f"{i}. [{h['session']}] score={h['score']} "
              f"(词面 {h['lexical']}, 向量 {h['vector']}, 新近 {h['recency']})")
        print(f"   {h['content'][:160]}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    from pdc.paths import skills_dir
    from pdc.skills import SkillRegistry

    registry = SkillRegistry(args.dir or skills_dir())
    if args.action == "list":
        print(json.dumps(
            {"count": len(registry), "skills": [m.to_dict() for m in registry.list()]},
            ensure_ascii=False, indent=2,
        ))
        return 0

    try:
        skill = registry.read(args.name)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"# {skill.meta.name}\n\n{skill.meta.description}\n")
    print(skill.body)
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    """变换字典：语料 + 官方文档 + schema 三方合成。

    为什么单独一个命令：
        字典是离线产物。构建一次，生成时只读 JSON，
        不联网、不调模型、不重新扫 5,831 条流水线。
    """
    from pdc import catalog as cat
    from pdc.parser import PipelineParseError, parse_pipeline

    if args.action == "docs":
        html_dir = Path(args.html_dir)
        if not html_dir.exists():
            print(f"找不到目录: {html_dir}", file=sys.stderr)
            return 2
        docs: dict = {}
        for page in sorted(html_dir.glob("*.html")):
            docs.update(cat.parse_doc_html(page.read_text(errors="ignore"), page.stem))
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(docs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"types": len(docs), "out": str(out)}, ensure_ascii=False, indent=2))
        return 0

    if args.action == "build":
        corpus_dir = Path(args.corpus)
        schema_path = Path(args.schema)
        if not corpus_dir.exists() or not schema_path.exists():
            print("--corpus 和 --schema 都必须存在", file=sys.stderr)
            return 2
        corpus = cat.scan_corpus(corpus_dir)
        docs = cat.load_docs(args.docs) if args.docs else {}
        sources = cat.scan_sources(args.sources) if args.sources else {}
        built = cat.build(
            corpus,
            docs,
            cat.load_schema(schema_path),
            sources,
        )
        out = cat.write(built, args.out)
        print(json.dumps({
            "pipelines": built["source"]["pipelines"],
            "transforms": built["source"]["transforms"],
            "types": len(built["transforms"]),
            "connectors": len(built["connectors"]),
            "source_files": len(sources),
            "out": str(out),
        }, ensure_ascii=False, indent=2))
        return 0

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        print(f"字典不存在: {catalog_path}，先跑 catalog build", file=sys.stderr)
        return 2
    built = cat.load(catalog_path)

    if args.action == "fill":
        import yaml as _yaml

        data = _yaml.safe_load(Path(args.target).read_text(encoding="utf-8"))
        filled_data, filled = cat.fill_defaults(data, built)
        # 默认只报告，不写回：YAML 往返会丢掉全部注释，
        # 真实流水线的注释比参数值更重要。
        if args.out and args.out != str(Path(cat.DEFAULT_PATH)):
            text = _yaml.safe_dump(filled_data, allow_unicode=True, sort_keys=False, width=200)
            Path(args.out).write_text(text, encoding="utf-8")
            print(json.dumps({
                "filled": filled,
                "out": args.out,
                "warning": "YAML 往返不保留注释",
            }, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"filled": filled}, ensure_ascii=False, indent=2))
        return 0

    if args.action == "prefer":
        names = [x.strip() for x in args.target.split(",") if x.strip()]
        rows = []
        for name in names:
            card = built["transforms"].get(name) or {}
            rows.append({
                "type": name,
                "count": int(card.get("count") or 0),
                "required": sorted(
                    k for k, v in (card.get("params") or {}).items()
                    if v.get("trust") == "required"
                ),
            })
        rows.sort(key=lambda r: -r["count"])
        if rows:
            rows[0]["preferred"] = True
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.action == "show":
        card = built["transforms"].get(args.target) or built["connectors"].get(args.target)
        if card is None:
            print(f"字典里没有 {args.target}", file=sys.stderr)
            return 2
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    try:
        facts = parse_pipeline(args.target)
    except PipelineParseError as exc:
        print(f"读不了 pipeline: {exc}", file=sys.stderr)
        return 2
    issues = cat.check(facts, built)
    print(json.dumps([i.to_dict() for i in issues], ensure_ascii=False, indent=2))
    return 1 if issues else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """检查配置：一次报全部问题，而不是启动后逐个撞。"""
    from pdc.config import ConfigError, Settings

    settings = Settings.load()
    print(json.dumps(settings.redacted(), ensure_ascii=False, indent=2))
    try:
        settings.validate()
    except ConfigError as exc:
        print("\n" + str(exc), file=sys.stderr)
        return 1
    print("\n配置检查通过。")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from pdc import backup

    if args.action == "create":
        result = backup.create(out_dir=args.out, keep=args.keep)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "verify":
        report = backup.verify(args.archive)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    if args.action == "restore":
        try:
            out = backup.restore(args.archive, args.to, force=args.force)
        except (FileExistsError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    raise SystemExit(f"未知动作: {args.action}")


def cmd_audit_log(args: argparse.Namespace) -> int:
    from pdc.audit_log import AuditLog

    log = AuditLog(args.db or None)
    if args.action == "verify":
        ok, bad = log.verify()
        print(json.dumps({"ok": ok, "first_bad_seq": bad}, ensure_ascii=False))
        return 0 if ok else 1
    print(json.dumps({"total": log.count(), "events": log.list(limit=args.limit)}, ensure_ascii=False, indent=2))
    return 0


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
        "--engine",
        choices=["react", "plan", "multi", "graph", "langchain"],
        default="react",
        help="执行范式：react / plan / multi / graph（LangGraph）/ langchain",
    )
    a.add_argument(
        "--session", default="",
        help="会话 ID。给了就启用记忆（短期窗口 + 摘要 + 落盘）",
    )
    a.set_defaults(func=cmd_agent)

    b = sub.add_parser("build", help="设计文档 -> pipeline YAML（确定性优先，模型兜底）")
    b.add_argument("source", help=".xlsx 设计文档，或 .md/.txt 需求文本")
    b.add_argument("--name", default="", help="自由文本模式下指定 pipeline 名称")
    b.add_argument("--out", default=str(DEFAULT_OUT))
    b.add_argument("--model", default="deepseek-flash")
    b.add_argument(
        "--engine", choices=["auto", "model"], default="auto",
        help="auto=认识的模板家族用计划器，其余用模型；model=强制走模型",
    )
    b.add_argument("--max-rounds", type=int, default=8)
    b.add_argument("--budget", type=int, default=0, help="token 预算，0 表示不限")
    b.add_argument("--dry-run", action="store_true", help="只打印读到的需求，不调模型")
    b.add_argument(
        "--examples", default="",
        help="参考示例目录。评测时用它做留一法，避免模型直接抄到源文件",
    )
    b.add_argument("--route", choices=["", "cost", "quality", "balanced"], default="")
    b.set_defaults(func=cmd_build)

    ix = sub.add_parser("index", help="建 pipeline 语义索引")
    ix.add_argument("action", nargs="?", default="build", choices=["build", "update"],
                    help="build=全量重建；update=只处理新增/变更/删除")
    ix.add_argument("--repo", default="samples/pipelines")
    ix.add_argument("--out", default="out/index")
    ix.add_argument("--backend", choices=["numpy", "sqlite-vec", "pgvector"], default="numpy")
    ix.add_argument("--dsn", default="", help="pgvector 的 Postgres DSN；也可用 PDC_PGVECTOR_DSN")
    ix.add_argument("--limit", type=int, default=0)
    ix.add_argument("--offline", action="store_true", help="用哈希向量，不下载模型")
    ix.set_defaults(func=cmd_index)

    se = sub.add_parser("search", help="语义检索 pipeline")
    se.add_argument("query")
    se.add_argument("--index", default="out/index")
    se.add_argument("-k", "--top-k", type=int, default=5)
    se.add_argument("--where", action="append", help="过滤，例如 --where chain=eid")
    se.add_argument("--offline", action="store_true")
    se.add_argument("--dsn", default="", help="pgvector 的 Postgres DSN")
    se.set_defaults(func=cmd_search)

    be = sub.add_parser("bench", help="比 numpy 与 sqlite-vec 的构建/查询成本")
    be.add_argument("--index", default="out/index")
    be.add_argument("--sizes", default="1000,10000,100000")
    be.add_argument("--queries", type=int, default=20)
    be.set_defaults(func=cmd_bench)

    mem = sub.add_parser("memory", help="长期记忆：列出会话 / 检索")
    mem.add_argument("action", choices=["list", "search"], nargs="?", default="list")
    mem.add_argument("query", nargs="?", default="")
    mem.add_argument("--dir", default="out/memory")
    mem.add_argument("--top", type=int, default=5)
    mem.add_argument("--no-vector", action="store_true")
    mem.set_defaults(func=cmd_memory)

    sk = sub.add_parser("skills", help="列出 / 查看 skill（渐进披露的元数据）")
    sk.add_argument("action", choices=["list", "show"], nargs="?", default="list")
    sk.add_argument("name", nargs="?", default="", help="show 时的 skill 名")
    sk.add_argument("--dir", default="", help="自定义 skills 目录")
    sk.set_defaults(func=cmd_skills)

    doc = sub.add_parser("doctor", help="检查配置（不启动服务）")
    doc.set_defaults(func=cmd_doctor)

    ca = sub.add_parser("catalog", help="变换字典：语料 + 文档 + schema")
    ca.add_argument("action", choices=["build", "docs", "show", "check", "fill", "prefer"])
    ca.add_argument("target", nargs="?", default="", help="show 的类型名，或 check 的 YAML 路径")
    ca.add_argument("--corpus", default="", help="真实流水线目录")
    ca.add_argument("--docs", default="", help="抓取阶段产出的 docs.json")
    ca.add_argument("--sources", default="", help="变换源码目录（__init__ 签名）")
    ca.add_argument("--schema", default="", help="pipeline-schema.json")
    ca.add_argument("--html-dir", default="", help="docs 动作：抓下来的 HTML 目录")
    ca.add_argument("--out", default="out/catalog.json")
    ca.add_argument("--catalog", default="out/catalog.json")
    ca.set_defaults(func=cmd_catalog)

    bk = sub.add_parser("backup", help="备份 / 校验 / 恢复 SQLite")
    bk.add_argument("action", choices=["create", "verify", "restore"])
    bk.add_argument("--out", default="backups")
    bk.add_argument("--keep", type=int, default=7)
    bk.add_argument("--archive", default="")
    bk.add_argument("--to", default="restored")
    bk.add_argument("--force", action="store_true")
    bk.set_defaults(func=cmd_backup)

    al = sub.add_parser("audit-log", help="审计日志：列表 / 校验哈希链")
    al.add_argument("action", choices=["list", "verify"], nargs="?", default="list")
    al.add_argument("--db", default="")
    al.add_argument("--limit", type=int, default=20)
    al.set_defaults(func=cmd_audit_log)

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
