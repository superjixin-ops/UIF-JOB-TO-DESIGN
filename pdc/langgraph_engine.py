"""LangGraph 版执行引擎：和手写 ReAct 循环做对照。

为什么还要留着手写版：
    手写版能看清每一步的状态转移；LangGraph 版能拿到 checkpointer、
    可视化、中断恢复。两者不是替代关系，是不同阶段的工具。

概念映射（面试可以直接说这张表）：

    手写版                       LangGraph
    ─────────────────────────    ──────────────────────────
    messages 列表                State（TypedDict）
    while 循环                   graph 的边
    if not tool_calls: return    条件边 should_continue
    工具执行函数                 ToolNode（这里手写节点）
    max_steps 计数               recursion_limit
    自己序列化状态               checkpointer（MemorySaver）

收益不是"更聪明"，是**可中断、可回放、可视化**。
代价是多一层抽象，出错时栈更深。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from pdc.llm import LLM, LLMReply
from pdc.tools import Toolbox


class AgentState(TypedDict, total=False):
    messages: Annotated[list[dict], "对话历史，含 tool_calls 与 tool 消息"]
    steps: int
    last_reply: dict
    output: str


def _reply_to_message(reply: LLMReply) -> dict:
    return {
        "role": "assistant",
        "content": reply.content or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
            }
            for c in reply.tool_calls
        ] or None,
    }


def build_graph(llm: LLM, toolbox: Toolbox, *, max_steps: int = 15):
    """把 ReAct 循环编译成图。

    节点只有两个：agent（问模型）和 tools（执行工具）。
    终止条件在条件边上，不在节点里——这是图和循环最本质的区别。
    """
    schemas = toolbox.tool_schemas()

    tracer = toolbox.tracer

    def agent_node(state: AgentState) -> dict:
        from pdc.trace import Timer

        with Timer() as t:
            reply = llm.chat(state["messages"], schemas)
        tracer.record(
            "llm", getattr(llm, "name", "llm"),
            input=state["messages"][-1].get("content", ""),
            output=reply.content,
            latency_ms=t.elapsed_ms,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
        )
        return {
            "messages": state["messages"] + [_reply_to_message(reply)],
            "steps": state.get("steps", 0) + 1,
            "last_reply": {
                "content": reply.content or "",
                "tool_calls": [c.__dict__ for c in (reply.tool_calls or [])],
            },
        }

    def tools_node(state: AgentState) -> dict:
        reply = state.get("last_reply") or {}
        new_messages = []
        for raw in reply.get("tool_calls", []):
            name = raw["name"]
            result = toolbox.call(name, raw.get("arguments") or {})  # 内部已写 trace
            new_messages.append({
                "role": "tool",
                "tool_call_id": raw["id"],
                "content": json.dumps(result.to_json(), ensure_ascii=False, default=str)[:8000],
            })
        return {"messages": state["messages"] + new_messages}

    def should_continue(state: AgentState) -> str:
        reply = state.get("last_reply") or {}
        if state.get("steps", 0) >= max_steps:
            return END
        return "tools" if reply.get("tool_calls") else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def run_graph(llm: LLM, toolbox: Toolbox, goal: str, *, max_steps: int = 15,
              system: str | None = None, checkpointer=None) -> dict:
    """执行图。返回 {output, steps, messages}。"""
    from pdc.agent import SYSTEM_PROMPT

    app = build_graph(llm, toolbox, max_steps=max_steps)
    initial: AgentState = {
        "messages": [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ],
        "steps": 0,
    }

    config: dict[str, Any] = {"recursion_limit": max_steps * 2 + 2}
    if checkpointer is not None:
        config["configurable"] = {"thread_id": "pdc"}

    final = app.invoke(initial, config=config)
    reply = final.get("last_reply") or {}
    return {
        "output": reply.get("content", ""),
        "steps": final.get("steps", 0),
        "messages": final.get("messages", []),
    }


def make_checkpointer():
    """内存检查点：进程内可回放/中断。生产要换成 SqliteSaver 之类的持久化实现。"""
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()
