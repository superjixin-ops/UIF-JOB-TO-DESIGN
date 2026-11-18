"""对比两次运行：模型引用的规则，是否真的被检索到过。

这是一个比"引用是否存在"更硬的指标：
    引用存在      —— 没有编造文件名和标题
    引用被检索到  —— 结论确实建立在工具返回值上，而不是模型记忆

后者才是"检索是否真的帮到了 Agent"的证据。

用法：
    uv run python -m rag.analyze out/auto-bm25/*trace.jsonl
    uv run python -m rag.analyze out/auto-hybrid/*trace.jsonl
    uv run python -m rag.analyze --compare out/auto-bm25/x.jsonl out/auto-hybrid/y.jsonl
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.citations import CITE_RE, RuleCitations, _norm  # noqa: E402

ANCHOR_RE = re.compile(r"[0-9]{2}-[a-z0-9-]+#[A-Za-z0-9][A-Za-z0-9 ._/-]{0,60}")


def read_run(trace_path: Path) -> dict:
    steps = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    final = ""
    retrieved: set[str] = set()
    search_calls = 0
    queries: list[str] = []

    for s in steps:
        if s.get("kind") == "llm" and s.get("output"):
            final = s["output"]
        if s.get("kind") == "tool" and s.get("name") == "search_rules":
            search_calls += 1
            args = s.get("input") or {}
            if isinstance(args, dict) and args.get("query"):
                queries.append(str(args["query"]))
            out = s.get("output")
            if isinstance(out, list):
                for hit in out:
                    if isinstance(hit, dict) and hit.get("id"):
                        retrieved.add(hit["id"])
            elif isinstance(out, str):
                # 兼容旧 trace（被截断成字符串）
                for cid in re.findall(r'"id":\s*"([^"]+)"', out):
                    retrieved.add(cid)

    citations = sorted({c.strip() for c in ANCHOR_RE.findall(final)})
    return {
        "trace": str(trace_path),
        "final_chars": len(final),
        "search_calls": search_calls,
        "queries": queries,
        "retrieved": sorted(retrieved),
        "citations": citations,
        "final": final,
    }


def was_retrieved(citation: str, retrieved: set[str]) -> bool:
    """引用是否命中本次实际检索到的 chunk。"""
    m = re.match(r"([0-9]{2}-[a-z0-9-]+)#(.+)", citation)
    if not m:
        return False
    stem, anchor = m.group(1), _norm(m.group(2))
    for cid in retrieved:
        if "#" not in cid:
            continue
        cfile, cheading = cid.split("#", 1)
        if cfile != stem:
            continue
        if _norm(cheading).startswith(anchor) or anchor.startswith(_norm(cheading)[: max(len(anchor) - 5, 8)]):
            return True
    return False


def report(run: dict, rc: RuleCitations) -> dict:
    valid = [c for c in run["citations"] if not rc.check(c)]
    grounded = [c for c in run["citations"] if was_retrieved(c, set(run["retrieved"]))]
    return {
        "trace": run["trace"],
        "search_calls": run["search_calls"],
        "retrieved_chunks": len(run["retrieved"]),
        "citations": len(run["citations"]),
        "citations_valid": len(valid),
        "citations_retrieved": len(grounded),
        "retrieval_grounding": round(len(grounded) / max(len(run["citations"]), 1), 3),
        "queries": run["queries"],
        "cited": run["citations"],
        "cited_but_not_retrieved": [c for c in run["citations"] if c not in grounded],
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    rc = RuleCitations()

    if "--compare" in argv:
        i = argv.index("--compare")
        a, b = Path(argv[i + 1]), Path(argv[i + 2])
        ra, rb = report(read_run(a), rc), report(read_run(b), rc)
        print(f"{'指标':<24} {'A':>8} {'B':>8}")
        print("-" * 42)
        for k in ("search_calls", "retrieved_chunks", "citations", "citations_valid",
                  "citations_retrieved", "retrieval_grounding"):
            print(f"{k:<24} {ra[k]:>8} {rb[k]:>8}")
        print(f"\nA 引用未检索到: {ra['cited_but_not_retrieved']}")
        print(f"B 引用未检索到: {rb['cited_but_not_retrieved']}")
        return 0

    for arg in argv:
        r = report(read_run(Path(arg)), rc)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
