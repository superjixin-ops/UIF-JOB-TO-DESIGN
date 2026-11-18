"""ReAct 主循环。

三个版本在同一个文件里，方便对比：
  run_once     —— 单次调用，无工具（基线）
  run_agent    —— ReAct 循环 + 工具 + trace + 预算
  run_with_self_check —— 在 run_agent 外面套一层"生成→校验→修复"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pdc.llm import LLM
from pdc.tools import Toolbox, ToolResult
from pdc.trace import Timer, Tracer

SYSTEM_PROMPT = """你是 ETL 设计文档生成专家。你的工作是把 UIF pipeline 的结构化事实转换成可评审的设计文档。

必须遵守的规则：
1. 只使用工具返回的事实。禁止凭印象补充。
2. 涉及列数、列缺失、差异判断时，必须调用 diff_schema。禁止自己数。
3. 写文档前必须先 inspect_pipeline 拿事实，再 build_design。
4. write_document 之后必须调用 validate_document 自检。
5. 如果 validate_document 返回 issues，必须描述你打算怎么修，并重新生成。
6. 事实不足时写 "not asserted"，不要编造。

工作顺序（除非用户另有要求）：
  inspect_pipeline -> diff_schema -> search_rules -> build_design
  -> write_document -> validate_document -> 汇报结果

汇报时给出：生成的文件路径、发现的高危问题数量、你判断需要人工确认的点。"""


@dataclass
class AgentResult:
    output: str
    steps: int
    trace: Tracer
    written: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


def run_once(llm: LLM, goal: str) -> str:
    """基线：没有任何工具。用来证明"不给工具，模型只能编"。"""
    reply = llm.chat([{"role": "user", "content": goal}], [])
    return reply.content


def run_agent(
    llm: LLM,
    toolbox: Toolbox,
    goal: str,
    *,
    max_steps: int = 15,
    max_tool_result_chars: int = 8000,
    user_context: dict | None = None,
    system: str | None = None,
    schemas: list[dict] | None = None,
) -> AgentResult:
    tracer = toolbox.tracer
    messages: list[dict] = [
        {"role": "system", "content": system or SYSTEM_PROMPT},
        {"role": "user", "content": goal, "context": user_context or {}},
    ]
    tool_schemas = schemas if schemas is not None else Toolbox.schemas()
    written: dict = {}
    issues: list[str] = []

    for step in range(1, max_steps + 1):
        with Timer() as t:
            reply = llm.chat(messages, tool_schemas)
        tracer.record(
            "llm",
            getattr(llm, "name", "llm"),
            input=messages[-1].get("content", ""),
            output=reply.content,
            latency_ms=t.elapsed_ms,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
        )

        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                    }
                    for c in reply.tool_calls
                ]
                or None,
            }
        )

        if not reply.tool_calls:
            return AgentResult(
                output=reply.content or "（模型未给出结论）",
                steps=step,
                trace=tracer,
                written=written,
                issues=issues,
            )

        for call in reply.tool_calls:
            result: ToolResult = toolbox.call(call.name, call.arguments)
            payload = result.to_json()

            if result.ok and call.name == "write_document" and isinstance(result.data, dict):
                written.update(result.data)
            if result.ok and call.name == "validate_document" and isinstance(result.data, dict):
                issues = result.data.get("issues", [])

            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(text) > max_tool_result_chars:
                text = text[:max_tool_result_chars] + f"...<truncated {len(text) - max_tool_result_chars} chars>"

            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    tracer.record("final", "max_steps_reached", output=f"达到最大步数 {max_steps}")
    return AgentResult(
        output=f"达到最大步数 {max_steps}，未收敛。已生成：{written or '无'}",
        steps=max_steps,
        trace=tracer,
        written=written,
        issues=issues,
    )


def run_with_self_check(
    llm: LLM,
    toolbox: Toolbox,
    goal: str,
    *,
    max_rounds: int = 3,
    **kwargs,
) -> AgentResult:
    """生成 -> 校验 -> 把 issue 喂回去修 -> 再校验。

    校验器是确定性的，所以这个循环是可信的；
    模型的自评不可信，所以不能只靠"你检查一下"。
    """
    result = run_agent(llm, toolbox, goal, **kwargs)
    rounds = 0

    while result.issues and rounds < max_rounds:
        rounds += 1
        fix = (
            "上一版输出有以下问题，请修正后重新生成，不要解释，直接执行：\n"
            + "\n".join(f"- {i}" for i in result.issues)
        )
        result = run_agent(llm, toolbox, f"{goal}\n\n{fix}", **kwargs)

    return result


def load_trace(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
