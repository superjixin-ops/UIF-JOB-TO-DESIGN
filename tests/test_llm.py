"""真实客户端路径的测试：不联网，但走完整的请求构造和响应解析。

为什么必须有这组测试：
  之前所有循环测试都用 ScriptedLLM，等于只测了"假模型"。
  真模型这条路径上的坑（消息格式、tool_calls 解析、脏 JSON、重试）
  一个都没覆盖。这些恰恰是上线后最先炸的地方。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdc.llm import OpenAICompatibleLLM, _safe_json, _sanitize


# ---------------------------------------------------------------- 消息清洗
def test_sanitize_strips_local_keys():
    messages = [
        {"role": "user", "content": "hi", "context": {"pipeline": "x.yaml"}},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
    ]
    clean = _sanitize(messages)
    assert "context" not in clean[0]
    assert clean[0]["content"] == "hi"
    assert clean[1]["tool_calls"]
    assert clean[2]["tool_call_id"] == "1"
    # provider 只认这些键
    allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
    for m in clean:
        assert set(m) <= allowed


# ---------------------------------------------------------------- 脏 JSON
def test_safe_json_plain():
    assert _safe_json('{"a": 1}') == {"a": 1}


def test_safe_json_with_surrounding_prose():
    raw = '好的，我调用了工具：\n{"name": "inspect", "args": {"x": 1}}\n以上。'
    assert _safe_json(raw) == {"name": "inspect", "args": {"x": 1}}


def test_safe_json_empty_and_broken():
    assert _safe_json(None) == {}
    assert _safe_json("") == {}
    assert "__unparsed__" in _safe_json("完全不是 json")


# ---------------------------------------------------------------- 假 client
class _FakeCompletions:
    def __init__(self, behaviour):
        self._behaviour = list(behaviour)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._behaviour.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, behaviour):
        self.chat = SimpleNamespace(completions=_FakeCompletions(behaviour))


def _reply(content: str = "", calls: list[tuple[str, str, str]] | None = None):
    tool_calls = [
        SimpleNamespace(id=i, function=SimpleNamespace(name=n, arguments=a))
        for i, n, a in (calls or [])
    ]
    msg = SimpleNamespace(content=content or None, tool_calls=tool_calls or None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


def test_client_parses_content_and_usage():
    llm = OpenAICompatibleLLM("m", client=_FakeClient([_reply("done")]))

    out = llm.chat([{"role": "user", "content": "hi"}], [])
    assert out.content == "done"
    assert out.tool_calls == []
    assert out.tokens_in == 11 and out.tokens_out == 7


def test_client_parses_tool_calls():
    llm = OpenAICompatibleLLM("m", client=_FakeClient([
        _reply(calls=[("call_1", "diff_schema", '{"name": "a.yaml", "job_index": 0}')])
    ]))

    out = llm.chat([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert len(out.tool_calls) == 1
    call = out.tool_calls[0]
    assert call.name == "diff_schema"
    assert call.arguments == {"name": "a.yaml", "job_index": 0}


def test_client_retries_then_succeeds(monkeypatch):
    # 别真的 sleep
    monkeypatch.setattr("pdc.llm.time.sleep", lambda *_: None)

    llm = OpenAICompatibleLLM("m", max_retries=3, client=_FakeClient(
        [RuntimeError("429"), RuntimeError("timeout"), _reply("ok")]
    ))

    out = llm.chat([{"role": "user", "content": "hi"}], [])
    assert out.content == "ok"
    assert len(llm._client.chat.completions.calls) == 3


def test_client_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr("pdc.llm.time.sleep", lambda *_: None)

    llm = OpenAICompatibleLLM("m", max_retries=2, client=_FakeClient(
        [RuntimeError("boom"), RuntimeError("boom")]
    ))

    with pytest.raises(RuntimeError, match="LLM 调用失败"):
        llm.chat([{"role": "user", "content": "hi"}], [])


def test_request_payload_is_clean():
    """发给 provider 的 messages 里不能有本地键。"""
    llm = OpenAICompatibleLLM("m", temperature=0.15, client=_FakeClient([_reply("ok")]))

    llm.chat(
        [{"role": "user", "content": "go", "context": {"pipeline": "x"}}],
        [{"type": "function", "function": {"name": "f"}}],
    )

    sent = llm._client.chat.completions.calls[0]
    assert "context" not in sent["messages"][0]
    assert sent["temperature"] == 0.15
    assert sent["tools"] == [{"type": "function", "function": {"name": "f"}}]
