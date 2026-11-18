"""工具层：Agent 唯一能改变世界的入口。

设计规则：
  1. 每个工具是纯函数（除了 write_* 有 IO），可单测
  2. 返回值必须是 JSON 可序列化，且带 ok/error 结构
  3. 只暴露"模型必须判断"的能力；精确计算全部内置在工具里
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pdc.checks import schema_diff as _schema_diff
from pdc.design import build_design
from pdc.grounding import ground, grounding_score
from pdc.models import PipelineFacts
from pdc.parser import PipelineParseError, parse_pipeline
from pdc.paths import out_dir as default_out_dir
from pdc.paths import repo_dir as default_repo_dir
from pdc.rag import get_retriever
from pdc.render import render_review_md, render_xlsx
from pdc.trace import Tracer
from pdc.validate import validate_doc

DEFAULT_REPO = default_repo_dir()


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str = ""

    def to_json(self) -> dict:
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error": self.error}


class Toolbox:
    """持有运行状态（缓存、产物路径），并把调用写进 trace。"""

    def __init__(
        self,
        repo: Path | None = None,
        out_dir: Path | None = None,
        tracer: Tracer | None = None,
        rules_dir: Path | None = None,
    ) -> None:
        self.repo = Path(repo or DEFAULT_REPO)
        self.out_dir = Path(out_dir or default_out_dir())
        self.tracer = tracer or Tracer()
        self._facts: dict[str, PipelineFacts] = {}
        self._docs: dict[str, Any] = {}
        self._paths: dict[str, dict[str, str]] = {}
        if rules_dir is not None:
            # 测试或自定义知识库：不加载向量，避免依赖网络
            from pdc.rag import HybridRetriever, RuleIndex

            self.rules = HybridRetriever(RuleIndex(rules_dir), enabled=False)
        else:
            # 默认走混合检索（α=0.1）；PDC_RAG_MODE=bm25 可降级
            self.rules = get_retriever()
        self._registry: dict[str, Callable[..., Any]] = {
            "list_pipelines": self.list_pipelines,
            "inspect_pipeline": self.inspect_pipeline,
            "diff_schema": self.diff_schema,
            "search_rules": self.search_rules,
            "build_design": self.build_design,
            "write_document": self.write_document,
            "validate_document": self.validate_document,
        }

    # ---------------- 工具实现 ----------------

    def list_pipelines(self, keyword: str = "", limit: int = 50) -> list[str]:
        hits: list[str] = []
        kw = keyword.lower()
        for p in self.repo.rglob("*.yaml"):
            if kw and kw not in p.name.lower():
                continue
            hits.append(str(p.relative_to(self.repo)))
            if len(hits) >= limit:
                break
        return sorted(hits)

    def _facts_of(self, name: str) -> PipelineFacts:
        if name in self._facts:
            return self._facts[name]
        candidate = self.repo / name
        matches = [candidate] if candidate.exists() else list(self.repo.rglob(f"{name}"))
        if not matches:
            raise FileNotFoundError(f"找不到 pipeline: {name}")
        facts = parse_pipeline(matches[0])
        self._facts[name] = facts
        return facts

    def inspect_pipeline(self, name: str) -> dict:
        facts = self._facts_of(name)
        return {
            "name": facts.name,
            "kind": facts.kind,
            "owner": facts.owner,
            "source_path": facts.source_path,
            "jobs": [
                {
                    "job_type": j.job_type,
                    "size": j.size,
                    "extracts": [
                        {"alias": e.alias, "connector": e.connector,
                         "main_df": e.main_df, "query": e.query}
                        for e in j.extracts
                    ],
                    "transform_count": len(j.transforms),
                    "transform_types": sorted({t.transform_type for t in j.transforms}),
                    "load": None if not j.load else {
                        "object": j.load.object_name,
                        "mode": j.load.mode,
                        "partitions": j.load.partitions,
                        "columns": [c.name for c in j.load.schema],
                        "partition_by": list(j.load.partition_by),
                    },
                }
                for j in facts.jobs
            ],
        }

    def diff_schema(self, name: str, job_index: int = 0) -> list[str]:
        facts = self._facts_of(name)
        return _schema_diff(facts, job_index)

    def search_rules(self, query: str, top_k: int = 5) -> list[dict]:
        hits = self.rules.search(query, top_k=top_k)
        if getattr(self.rules, "degraded", False):
            # 让模型和调用方都知道当前是降级状态，而不是静默变差
            for h in hits:
                h["degraded"] = True
        return hits

    def build_design(self, name: str) -> dict:
        facts = self._facts_of(name)
        doc = build_design(facts)
        self._docs[name] = doc
        return doc.to_dict()

    def write_document(self, name: str) -> dict:
        if name not in self._docs:
            self.build_design(name)
        doc = self._docs[name]
        xlsx = render_xlsx(doc, self.out_dir / f"{doc.pipeline}-design.xlsx")
        md = render_review_md(doc, self.out_dir / f"{doc.pipeline}-review.md")
        self._paths[name] = {"xlsx": str(xlsx), "review": str(md)}
        return self._paths[name]

    def validate_document(self, name: str) -> dict:
        paths = self._paths.get(name)
        if not paths:
            raise RuntimeError("还没有生成文档，先调用 write_document")
        doc = self._docs[name]
        facts = self._facts_of(name)

        # 第一层：结构契约（渲染出来的文件 vs IR）
        structural = validate_doc(doc, paths["xlsx"])
        # 第二层：事实对齐（文档里的断言 vs 解析结果）
        violations = ground(doc, facts, self.rules.rule_ids)
        grounded = [str(v) for v in violations]

        return {
            "issues": structural + grounded,
            "structural_issues": structural,
            "grounding_violations": grounded,
            "grounding_score": grounding_score(violations, len(doc.findings)),
            "clean": not structural,
            "grounded": not violations,
        }

    # ---------------- 分发 ----------------

    def call(self, name: str, args: dict) -> ToolResult:
        fn = self._registry.get(name)
        if fn is None:
            return ToolResult(ok=False, error=f"未知工具: {name}")
        start = time.perf_counter()
        try:
            data = fn(**args)
            elapsed = int((time.perf_counter() - start) * 1000)
            self.tracer.tool_call(name, args, data, elapsed)
            return ToolResult(ok=True, data=data)
        except (TypeError, ValueError, KeyError, FileNotFoundError, RuntimeError, PipelineParseError) as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            msg = f"{type(exc).__name__}: {exc}"
            self.tracer.tool_call(name, args, None, elapsed, error=msg)
            return ToolResult(ok=False, error=msg)

    # ---------------- 给模型的 schema ----------------

    @staticmethod
    def schemas() -> list[dict]:
        def fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }

        return [
            fn(
                "list_pipelines",
                "按关键词搜索仓库中的 pipeline 文件名。当用户只给了模糊的名字、或需要确认某个流水线是否存在时调用。"
                "不要在已知确切文件名时调用。",
                {"keyword": {"type": "string", "description": "文件名片段，例如 marketing"},
                 "limit": {"type": "integer", "description": "最多返回条数，默认 50"}},
                [],
            ),
            fn(
                "inspect_pipeline",
                "解析 pipeline，返回结构化的 jobs / extracts / transforms 类型 / load schema。"
                "在撰写任何内容之前必须先调用本工具获取事实。禁止凭文件名猜测内容。",
                {"name": {"type": "string", "description": "例如 fact-order-fulfillment.yaml"}},
                ["name"],
            ),
            fn(
                "diff_schema",
                "比对第 job_index 个 job 的产出帧与下一个 job 的 load schema，返回差异清单。"
                "凡是涉及列数统计、列缺失判断，必须调用本工具，禁止自行数数。",
                {"name": {"type": "string"},
                 "job_index": {"type": "integer", "description": "从 0 开始，默认 0"}},
                ["name"],
            ),
            fn(
                "search_rules",
                "在 UIF 规则库中检索。当需要确认某个 transform 的参数含义、或者判断某条业务规则是否存在时调用。"
                "返回带 source 和 heading 的片段，引用时必须带 rule id。",
                {"query": {"type": "string", "description": "自然语言问题，中英文均可"},
                 "top_k": {"type": "integer", "description": "默认 5"}},
                ["query"],
            ),
            fn(
                "build_design",
                "根据事实生成设计文档的中间表示（IR）。这一步不写文件。"
                "如果 diff_schema 发现了问题，必须先调用本工具再决定如何在评审里描述。",
                {"name": {"type": "string"}},
                ["name"],
            ),
            fn(
                "write_document",
                "把 IR 渲染成 xlsx 和 review.md。有副作用，只在内容确认后调用。"
                "返回文件路径。",
                {"name": {"type": "string"}},
                ["name"],
            ),
            fn(
                "validate_document",
                "对已生成的文档做结构与契约校验（sheet 名、表头、列数与 load schema、编号连续性）。"
                "返回 issues 列表，clean 为 true 表示通过。每次 write 之后都要调用。",
                {"name": {"type": "string"}},
                ["name"],
            ),
        ]
