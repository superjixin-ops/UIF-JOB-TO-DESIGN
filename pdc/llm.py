"""模型客户端。

抽象成 Protocol，因为：
1. 测试时用 FakeLLM，不需要网络
2. 换 provider 只改一个类
3. offline 模式下用 ScriptedLLM（确定性策略），证明工具层与模型解耦
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str = ""


class LLMTruncated(RuntimeError):
    """输出被 max_tokens 截断。

    单独一个类型，是为了让上层能分清两种失败：
      - 模型不会写（内容错）—— 重试或改 prompt 有用
      - 输出被砍断（长度错）—— 重试没用，得调大上限或拆任务
    """


DEFAULT_MAX_OUTPUT_TOKENS = 8192
MAX_OUTPUT_ENV = "PDC_MAX_OUTPUT_TOKENS"


def max_output_tokens_env() -> int:
    """单次模型输出的 token 上限。0 表示不限制（沿用 provider 默认）。

    默认 8192 的依据：最大的样本约 1.1 万字节，生成同规模 YAML 约 3k token，
    留 2 倍余量；同时能挡住"模型失控输出十万 token"的成本。
    真正的最优值应该从评测报告里 tokens_out 的分布来定，不能拍脑袋。
    """
    try:
        return int(os.getenv(MAX_OUTPUT_ENV, str(DEFAULT_MAX_OUTPUT_TOKENS)))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_TOKENS


class LLM(Protocol):
    name: str

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


# --------------------------------------------------------------------------
# 真实客户端（OpenAI 兼容：OpenAI / DeepSeek / Qwen / 内部网关）
# --------------------------------------------------------------------------
class OpenAICompatibleLLM:
    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.2,
        max_retries: int = 3,
        max_output_tokens: int | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.name = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.max_output_tokens = (
            max_output_tokens if max_output_tokens is not None else max_output_tokens_env()
        )
        # 记录本次进程内单次输出 token 的最大值。
        # 用途：跑完评测后用它来定 PDC_MAX_OUTPUT_TOKENS，而不是拍脑袋。
        self.max_observed_out = 0
        if client is not None:
            # 测试注入用，避免依赖网络和真实 key
            self._client = client
            return
        from openai import OpenAI

        self._client = OpenAI(
            base_url=base_url or os.getenv("OPENAI_BASE_URL"),
            api_key=api_key or os.getenv("OPENAI_API_KEY") or "not-needed",
        )

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        last_error: Exception | None = None
        payload = _sanitize(messages)
        for attempt in range(self.max_retries):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": payload,
                    "tools": tools or None,
                    "temperature": self.temperature,
                }
                # 0 表示不设上限，沿用 provider 默认
                if self.max_output_tokens:
                    kwargs["max_tokens"] = self.max_output_tokens
                resp = self._client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                msg = choice.message
                finish = getattr(choice, "finish_reason", "") or ""
                # 截断的 YAML 一定不合法。与其让它流到校验器再白跑一轮，
                # 不如在这里说清楚：是输出上限太小，不是模型不会写。
                if finish == "length":
                    raise LLMTruncated(
                        f"模型输出被截断（finish_reason=length，"
                        f"max_tokens={self.max_output_tokens}）。"
                        f"调大 {MAX_OUTPUT_ENV}，或把任务拆小。"
                    )
                calls = [
                    ToolCall(
                        id=c.id,
                        name=c.function.name,
                        arguments=_safe_json(c.function.arguments),
                    )
                    for c in (msg.tool_calls or [])
                ]
                usage = getattr(resp, "usage", None)
                out_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
                self.max_observed_out = max(self.max_observed_out, int(out_tokens or 0))
                return LLMReply(
                    content=msg.content or "",
                    tool_calls=calls,
                    tokens_in=getattr(usage, "prompt_tokens", 0) if usage else 0,
                    tokens_out=out_tokens,
                    finish_reason=finish,
                )
            except LLMTruncated:
                # 不重试：同样的 prompt + 同样的上限，重试大概率再截断一次，
                # 白白多花一份钱。
                raise
            except Exception as exc:  # noqa: BLE001 - 这里就是要兜住所有 provider 异常
                last_error = exc
                if attempt == self.max_retries - 1:
                    break
                time.sleep(1.5 * (2**attempt) + random.random() * 0.4)
        raise RuntimeError(f"LLM 调用失败（重试 {self.max_retries} 次）: {last_error}")


def _sanitize(messages: list[dict]) -> list[dict]:
    """provider 只认标准字段，本地附加的 context 等键必须剥掉。"""
    out = []
    for m in messages:
        item = {"role": m["role"], "content": m.get("content") or ""}
        if m.get("tool_calls"):
            item["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            item["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            item["name"] = m["name"]
        if not item["content"] and not item.get("tool_calls"):
            item["content"] = ""
        out.append(item)
    return out


def _safe_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 模型偶尔会在 JSON 前后加解释文字，取第一个完整对象
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        return {"__unparsed__": raw}


# --------------------------------------------------------------------------
# 测试用的脚本模型
# --------------------------------------------------------------------------
class ScriptedLLM:
    """按预设脚本返回回复。用于测试循环逻辑，不验证模型能力。"""

    name = "scripted"

    def __init__(self, script: list[LLMReply]) -> None:
        self._script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        self.calls.append(messages)
        if not self._script:
            return LLMReply(content="（脚本已耗尽）")
        return self._script.pop(0)


# --------------------------------------------------------------------------
# offline 模式：确定性策略，走同一套工具
# --------------------------------------------------------------------------
class RuleBasedLLM:
    """不调用任何模型，按标准作业流程（SOP）决定下一步。

    存在的意义：
      1. 没有 API key 也能端到端跑通，验证工具层正确性
      2. 作为 Agent 的对照组——如果 SOP 能做到 90%，说明模型只值 10%
      3. 演示"能用确定性代码解决就别上模型"
    """

    name = "rule-based"

    SOP: ClassVar[list[tuple[str, Any]]] = [
        ("list_pipelines", lambda ctx: {"keyword": ctx.get("artifact", "")}),
        ("inspect_pipeline", lambda ctx: {"name": ctx["pipeline"]}),
        ("diff_schema", lambda ctx: {"name": ctx["pipeline"], "job_index": 0}),
        ("search_rules", lambda ctx: {"query": "schema 声明 列 契约", "top_k": 3}),
        ("build_design", lambda ctx: {"name": ctx["pipeline"]}),
        ("write_document", lambda ctx: {"name": ctx["pipeline"]}),
        ("validate_document", lambda ctx: {"name": ctx["pipeline"]}),
    ]

    def __init__(self) -> None:
        self._i = 0

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        if self._i >= len(self.SOP):
            return LLMReply(content="SOP 执行完毕，输出已生成。")

        # 先拿到运行时上下文（第一条带 context 的 user 消息），
        # 再看最近一次工具结果。两件事要分开找，不能在一个 reversed 循环里做。
        ctx: dict[str, Any] = {}
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("context"), dict):
                ctx.update(m["context"])
                break
        for m in reversed(messages):
            if m.get("role") == "tool":
                ctx.setdefault("last_tool", m.get("content", ""))
                break

        name, arg_fn = self.SOP[self._i]
        self._i += 1
        return LLMReply(
            content=f"执行 SOP 第 {self._i} 步：{name}",
            tool_calls=[ToolCall(id=f"call_{self._i}", name=name, arguments=arg_fn(ctx))],
        )


def build_llm(mode: str, model: str = "") -> LLM:
    if mode == "offline":
        return RuleBasedLLM()
    if mode == "scripted":
        return ScriptedLLM([])
    return OpenAICompatibleLLM(model)
