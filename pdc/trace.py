"""全链路追踪。Agent 出问题时，日志没用，你需要看决策链。

每条记录一行 JSON，方便 grep、jq、导入 SQLite。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self


@dataclass
class Step:
    index: int
    kind: str                 # llm / tool / validate / final
    name: str
    input: Any = None
    output: Any = None
    latency_ms: int = 0
    error: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class Tracer:
    path: Path | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    steps: list[Step] = field(default_factory=list)
    # 流式推送用：每记录一步就回调一次（SSE / 前端时间线）
    on_step: Callable[[Step], None] | None = None
    callback_errors: list[str] = field(default_factory=list)

    def record(
        self,
        kind: str,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        latency_ms: int = 0,
        error: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> Step:
        step = Step(
            index=len(self.steps) + 1,
            kind=kind,
            name=name,
            input=_trim(input, 2000),
            # 工具返回值是审计证据，不能截断成字符串，否则下游分析拿不到结构
            output=_trim(output, 50000 if kind == "tool" else 2000),
            latency_ms=latency_ms,
            error=error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self.steps.append(step)
        if self.on_step is not None:
            try:
                self.on_step(step)
            except Exception as exc:  # noqa: BLE001
                # 回调（SSE 推送）失败不能中断主流程，但要留痕
                self.callback_errors.append(f"{type(exc).__name__}: {exc}")
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"run_id": self.run_id, **step.__dict__}, ensure_ascii=False, default=str))
                fh.write("\n")
        return step

    def tool_call(self, name: str, args: dict, result: Any, elapsed_ms: int, error: str = "") -> Step:
        return self.record("tool", name, input=args, output=result,
                           latency_ms=elapsed_ms, error=error)

    def stats(self) -> dict:
        return {
            "run_id": self.run_id,
            "steps": len(self.steps),
            "tool_calls": sum(1 for s in self.steps if s.kind == "tool"),
            "llm_calls": sum(1 for s in self.steps if s.kind == "llm"),
            "errors": sum(1 for s in self.steps if s.error),
            "tokens_in": sum(s.tokens_in for s in self.steps),
            "tokens_out": sum(s.tokens_out for s in self.steps),
            "latency_ms": sum(s.latency_ms for s in self.steps),
        }


def _trim(value: Any, limit: int = 2000) -> Any:
    """trace 不能无限增长，超长内容截断。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...<{len(value) - limit} chars>"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) <= limit:
            return value
        return text[:limit] + "...<truncated>"
    return value


class Timer:
    def __enter__(self) -> Self:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = int((time.perf_counter() - self._start) * 1000)
