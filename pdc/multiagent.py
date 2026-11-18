"""多智能体：Supervisor + Worker。

什么时候真的需要多 Agent（面试 06 模块的标准答案）：

    唯一硬理由是**上下文隔离**，不是"更聪明"。
    当子任务需要看的资料会污染主任务上下文时，把它隔离出去才有收益。

    代价同样明确：通信损耗、调试困难、错误在 Agent 间传播。

所以这里的实现遵循三条：
    1. Worker 有独立上下文，只把**结论**交回 Supervisor
    2. Supervisor 不干事，只做路由和验收
    3. Worker 的工具集是白名单子集，防止越权
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pdc.agent import run_agent
from pdc.llm import LLM
from pdc.tools import Toolbox

SUPERVISOR_PROMPT = """你是调度员，不亲自干活。你有两个下属：

- auditor：检查配置，找出问题。工具：list_pipelines / inspect_pipeline / diff_schema / search_rules
- writer：生成并校验设计文档。工具：build_design / write_document / validate_document

你的工作：
1. 把任务拆给合适的下属，一次只派一个。
2. 收到结果后判断是否足够；不够就再派，够了就收尾。
3. 不要自己分析数据，也不要猜下属会做什么。

给下属的任务要具体：说清目标文件、要做什么、输出什么。
"""

WORKERS: dict[str, dict] = {
    "auditor": {
        "prompt": (
            "你是流水线审计员。只根据工具返回的事实下结论。\n"
            "发现问题时说清：严重度（high=静默失败 / check=需确认 / info=启发式）、证据文件。\n"
            "事实不足时明确写 not asserted。"
        ),
        "tools": ["list_pipelines", "inspect_pipeline", "diff_schema", "search_rules"],
    },
    "writer": {
        "prompt": (
            "你负责生成设计文档。必须先 inspect_pipeline 拿事实，再 build_design、"
            "write_document，最后 validate_document 自检。"
        ),
        "tools": ["inspect_pipeline", "diff_schema", "build_design", "write_document", "validate_document"],
    },
}


@dataclass
class Delegation:
    worker: str
    task: str
    output: str = ""
    steps: int = 0


@dataclass
class MultiResult:
    final: str
    delegations: list[Delegation] = field(default_factory=list)
    total_steps: int = 0


def _delegate_tool_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "delegate",
                "description": "把子任务交给一个下属执行。一次只派一个。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "worker": {"type": "string", "enum": list(WORKERS)},
                        "task": {"type": "string", "description": "具体任务，包含目标文件"},
                    },
                    "required": ["worker", "task"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "所有子任务完成后收尾，给出总结。",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _filter_schemas(names: list[str]) -> list[dict]:
    """按名字取工具子集。

    注意：schemas() 和 schemas(include_audit=True) 有重叠，
    直接相加会出现同名工具，DeepSeek 会报 "Tool names must be unique"。
    用字典按名字去重。
    """
    by_name = {
        s["function"]["name"]: s
        for s in Toolbox.schemas(include_audit=True)
    }
    return [by_name[n] for n in names if n in by_name]


def run_multi(
    llm: LLM,
    toolbox: Toolbox,
    goal: str,
    *,
    max_turns: int = 6,
    max_steps_per_worker: int = 6,
) -> MultiResult:
    """Supervisor 循环：派活 → 收结果 → 判断 → 再派或收尾。"""
    messages: list[dict] = [
        {"role": "system", "content": SUPERVISOR_PROMPT},
        {"role": "user", "content": goal},
    ]
    tools = _delegate_tool_schema()
    delegations: list[Delegation] = []
    total = 0

    for _ in range(max_turns):
        reply = llm.chat(messages, tools)
        calls = reply.tool_calls or []

        # 必须先把 assistant 的 tool_calls 回填进历史，
        # 否则后面的 tool 消息没有对应的请求方，API 会报
        # "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'"
        messages.append({
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
                }
                for c in calls
            ] or None,
        })

        if not calls:
            # Supervisor 直接给了结论，视为收尾
            return MultiResult(final=reply.content, delegations=delegations, total_steps=total)

        for call in calls:
            if call.name == "finish":
                summary = str(call.arguments.get("summary", ""))
                return MultiResult(final=summary, delegations=delegations, total_steps=total)

            if call.name != "delegate":
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"未知工具 {call.name}",
                })
                continue

            worker = str(call.arguments.get("worker", ""))
            task = str(call.arguments.get("task", ""))
            if worker not in WORKERS:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"没有这个下属：{worker}",
                })
                continue

            spec = WORKERS[worker]
            result = run_agent(
                llm,
                toolbox,
                task,
                max_steps=max_steps_per_worker,
                system=spec["prompt"],
                schemas=_filter_schemas(spec["tools"]),
            )
            delegation = Delegation(worker=worker, task=task, output=result.output, steps=result.steps)
            delegations.append(delegation)
            total += result.steps

            # 只把结论交回 Supervisor，不带 Worker 的中间上下文
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": f"[{worker}] {result.output[:1200]}",
            })

    return MultiResult(
        final="达到最大调度轮数，未收尾。已完成：" + "；".join(d.worker for d in delegations),
        delegations=delegations,
        total_steps=total,
    )
