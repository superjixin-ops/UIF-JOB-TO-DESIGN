"""记忆系统：短期窗口 + 摘要压缩 + 长期持久化。

为什么 Agent 需要记忆：
    没有记忆时，每次请求都是"第一次见面"——用户要重复交代背景，
    上一轮的结论拿不到，多轮任务根本做不了。

四层结构（对应面试 05 模块）：

    工作记忆  当前 prompt 里的内容，随请求消失
    短期记忆  最近 N 轮对话，滑窗保留
    摘要记忆  窗口溢出时把旧轮次压成一段话
    长期记忆  落盘到 JSONL，可按关键词检索历史会话

工程上的三个约束（面试常问）：
    1. token 预算 —— 不是按轮数截断，是按 token 截断
    2. 摘要要保留决策和约束，丢弃寒暄和重复
    3. 落盘要能审计，且不能写进密钥（见 safety.redact_secrets）
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pdc.safety import redact_secrets, sanitize


def estimate_tokens(text: str) -> int:
    """粗估：中英混合按 3 字符 / token。真实计费用官方 tokenizer，这里只做预算。"""
    return max(1, len(text) // 3)


@dataclass
class Turn:
    role: str          # user / assistant
    content: str
    ts: float
    tokens: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {"role": self.role, "content": self.content, "ts": self.ts, "tokens": self.tokens},
            ensure_ascii=False,
        )


class SessionStore:
    """长期记忆：每个会话一个 JSONL 文件。

    为什么用 JSONL 而不是数据库：
        会话量小（每人每天几条），JSONL 可 grep、可人工查看、无依赖。
        等真到几万会话再换 SQLite，那时索引才有意义。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
        return self.root / f"{safe or 'default'}.jsonl"

    def load(self, session_id: str) -> list[Turn]:
        p = self.path(session_id)
        if not p.exists():
            return []
        turns: list[Turn] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "summary":
                continue
            turns.append(Turn(d["role"], d["content"], d.get("ts", 0.0), d.get("tokens", 0)))
        return turns

    def load_summary(self, session_id: str) -> str:
        p = self.path(session_id)
        if not p.exists():
            return ""
        latest = ""
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "summary":
                latest = d.get("content", "")
        return latest

    def append(self, session_id: str, turn: Turn) -> None:
        with self.path(session_id).open("a", encoding="utf-8") as fh:
            fh.write(turn.to_json() + "\n")

    def save_summary(self, session_id: str, summary: str) -> None:
        with self.path(session_id).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"type": "summary", "content": summary, "ts": time.time()},
                ensure_ascii=False,
            ) + "\n")

    def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl"))


class Memory:
    """一次会话的记忆视图。"""

    def __init__(
        self,
        store: SessionStore,
        session_id: str = "default",
        *,
        max_tokens: int = 1500,
        keep_turns: int = 2,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.max_tokens = max_tokens
        self.keep_turns = keep_turns
        self.turns: list[Turn] = store.load(session_id)
        self.summary: str = store.load_summary(session_id)

    # ------------------------------------------------------------------
    def add(self, role: str, content: str) -> None:
        turn = Turn(
            role=role,
            content=redact_secrets(sanitize(content)),
            ts=time.time(),
        )
        turn.tokens = estimate_tokens(turn.content)
        self.turns.append(turn)
        self.store.append(self.session_id, turn)

    # ------------------------------------------------------------------
    def overflow(self) -> bool:
        return self.used_tokens() > self.max_tokens

    def used_tokens(self) -> int:
        return sum(t.tokens for t in self.turns) + estimate_tokens(self.summary)

    def context(self) -> str:
        """拼成一段可以注入 prompt 的历史。

        顺序很重要：摘要在前（背景），最近轮次在后（细节）。
        模型对 prompt 末尾更敏感，所以最近的放最后。
        """
        parts: list[str] = []
        if self.summary:
            parts.append(f"[历史摘要]\n{self.summary}")
        recent = self.turns[-self.keep_turns:] if self.keep_turns else []
        if recent:
            lines = [f"{t.role}: {t.content[:300]}" for t in recent]
            parts.append("[最近对话]\n" + "\n".join(lines))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    def compact(self, summarize: Callable[[str], str]) -> str:
        """把旧轮次压成摘要。

        只保留最近 keep_turns 轮，其余交给模型总结。
        摘要要求：保留决策、约束、未完成事项；丢弃寒暄和重复。
        """
        if len(self.turns) <= self.keep_turns:
            return self.summary

        old = self.turns[: -self.keep_turns]
        recent = self.turns[-self.keep_turns:]
        transcript = "\n".join(f"{t.role}: {t.content}" for t in old)

        prompt = (
            "把下面的对话压成一段摘要，供后续继续这个任务时使用。\n"
            "必须保留：已经做出的决定、发现的结论、用户给出的约束、未完成的事项。\n"
            "可以丢弃：寒暄、重复解释、已经被推翻的中间结论。\n"
            "不超过 200 字，不要加前言。\n\n"
            f"{transcript}"
        )
        new_summary = summarize(prompt).strip()
        merged = (self.summary + "\n" + new_summary).strip() if self.summary else new_summary

        self.summary = merged
        self.turns = recent
        self.store.save_summary(self.session_id, merged)
        return merged


# ---------------------------------------------------------------------------
# 长期记忆检索：在历史会话里找相似片段
# ---------------------------------------------------------------------------
def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    buf: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            buf.append(ch)
        else:
            if buf:
                out.add("".join(buf))
                buf = []
    if buf:
        out.add("".join(buf))
    return {t for t in out if len(t) > 1}


def recall(store: SessionStore, query: str, top_k: int = 3) -> list[dict]:
    """在历史会话里按词面重合度检索。零依赖，够用。

    为什么不上向量：历史会话的写法高度重复（都是同一个人的技术讨论），
    词面匹配已经够；等出现"同义不同词"的漏检再加向量。
    """
    q = _tokens(query)
    if not q:
        return []

    scored: list[tuple[float, str, Turn]] = []
    for sid in store.list_sessions():
        for turn in store.load(sid):
            t = _tokens(turn.content)
            if not t:
                continue
            overlap = len(q & t) / len(q)
            if overlap > 0:
                scored.append((overlap, sid, turn))

    scored.sort(key=lambda x: -x[0])
    return [
        {
            "session": sid,
            "role": turn.role,
            "content": turn.content[:400],
            "score": round(score, 3),
        }
        for score, sid, turn in scored[:top_k]
    ]
