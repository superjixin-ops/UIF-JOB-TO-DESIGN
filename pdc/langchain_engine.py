"""LangChain 版本：用框架跑同一套工具，和手写循环做对照。

为什么加这个引擎，而不是把整个项目改成 LangChain：

    这个项目的核心论点是"控制流要可解释、可校验"。
    手写循环能看清每一步；LangChain 把循环藏进 AgentExecutor/Graph 里，
    调试时要多翻一层。

    但招聘要求里 70% 写了 LangChain。所以做法是：
    **同一套工具、同一个目标，多一个引擎，用同一套指标对比。**

为什么不直接用 `create_tool_calling_agent` + `AgentExecutor`：

    LangChain 1.x 已经移除了 AgentExecutor，`create_agent` 返回的是
    LangGraph 编译后的图。这不是"降级用法"，是官方现在的主路径。

和 pdc/agent.py 的关系：

    工具实现完全复用（Toolbox）。这里只做两件事：
      1. 把 Toolbox 的 JSON Schema 转成 LangChain 的 StructuredTool
      2. 把图执行的结果翻译回统一格式

    所以两个引擎的差异只在"谁决定下一步"，不在"能做什么"。
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import create_model

from pdc.tools import Toolbox

TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

SYSTEM = """你是 ETL 设计助手。必须遵守：
1. 只使用工具返回的事实，禁止凭印象补充。
2. 需要精确数字（列数、差异）时必须调工具。
3. 事实不足时明确说"需要人工确认"，不要编造。
4. 完成后给出结论和产物路径。
"""


def _args_model(tool_name: str, parameters: dict) -> type:
    """把 OpenAI 风格的 JSON Schema 转成 pydantic 模型。

    LangChain 的 StructuredTool 用 pydantic 校验参数；
    直接传 `**kwargs` 的函数会让它推断失败。
    """
    props = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        py_type = TYPE_MAP.get(str(spec.get("type", "string")), str)
        if key in required:
            fields[key] = (py_type, ...)
        else:
            fields[key] = (py_type | None, None)
    return create_model(f"{tool_name}_args", **fields)


def build_tools(toolbox: Toolbox) -> list:
    """把 Toolbox 的工具转成 LangChain StructuredTool。

    转换而不是重写：工具行为必须完全一致，否则对比没有意义。
    """
    from langchain_core.tools import StructuredTool

    tools = []
    for schema in toolbox.tool_schemas():
        fn = schema["function"]
        name = fn["name"]
        args_model = _args_model(name, fn.get("parameters") or {})

        def _call(_name: str = name, **kwargs: Any) -> str:
            # pydantic 会把"未传的可选参数"补成 None，
            # 而工具实现里可能直接 .lower()。适配层负责丢掉 None，
            # 而不是让每个工具都写一遍 None 判断。
            clean = {k: v for k, v in kwargs.items() if v is not None}
            result = toolbox.call(_name, clean)
            # Toolbox 返回的是 ToolResult 数据类，不是 dict；
            # 直接 json.dumps 会得到 "ToolResult(...)" 这种不可读字符串。
            payload = result.to_json() if hasattr(result, "to_json") else result
            return json.dumps(payload, ensure_ascii=False, default=str)

        tools.append(
            StructuredTool.from_function(
                func=_call,
                name=name,
                description=fn.get("description", ""),
                args_schema=args_model,
            )
        )
    return tools


class _TracerCallback:
    """把 LangChain 的模型调用记进我们自己的 Tracer。

    不做这件事，两个引擎的 trace 不可比：
    手写引擎能看到 9 次 llm、LangChain 看到 0 次——
    不是它没调，是调用没经过我们的记录点。
    """

    def __new__(cls, tracer):
        from langchain_core.callbacks import BaseCallbackHandler

        class Handler(BaseCallbackHandler):
            def __init__(self) -> None:
                self.tracer = tracer

            def on_llm_end(self, response, **kwargs) -> None:
                usage = {}
                llm_output = getattr(response, "llm_output", None) or {}
                usage = llm_output.get("token_usage") or {}
                self.tracer.record(
                    "llm",
                    "langchain",
                    output="",
                    tokens_in=int(usage.get("prompt_tokens", 0)),
                    tokens_out=int(usage.get("completion_tokens", 0)),
                )

        return Handler()


def run_langchain(
    toolbox: Toolbox,
    goal: str,
    *,
    model: str = "deepseek-chat",
    max_steps: int = 10,
) -> dict:
    """用 LangChain 的 create_agent 跑一遍。

    返回统一结构，方便和手写引擎对比：output / steps / tool_calls。
    """
    from langchain.agents import create_agent
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LangChain 引擎需要模型 key：设置 OPENAI_API_KEY（或 DEEPSEEK_API_KEY）"
        )

    llm = ChatOpenAI(
        model=model,
        temperature=0,
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL") or None,
    )
    agent = create_agent(model=llm, tools=build_tools(toolbox), system_prompt=SYSTEM)

    # LangGraph 的 recursion_limit 数的是"超步"：
    # 一次模型调用 + 一次工具执行大约占 2 步。按 max_steps * 2 设会刚好卡住，
    # 实测 6 步就报 GraphRecursionError。给 4 倍余量。
    from langgraph.errors import GraphRecursionError

    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": goal}]},
            config={
                "recursion_limit": max(20, max_steps * 4),
                "callbacks": [_TracerCallback(toolbox.tracer)],
            },
        )
    except GraphRecursionError:
        # 和手写引擎的 max_steps 语义对齐：达到上限要返回，不是抛异常。
        return {
            "output": f"达到递归上限（约 {max_steps} 步），未收敛。",
            "steps": max_steps,
            "tool_calls": 0,
        }
    messages = result.get("messages") or []
    final = ""
    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if content and getattr(msg, "type", "") in ("ai", "assistant"):
            final = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            break

    tool_calls = sum(len(getattr(m, "tool_calls", []) or []) for m in messages)
    return {
        "output": final or "（图执行结束，无最终文本）",
        "steps": len(messages),
        "tool_calls": tool_calls,
    }
