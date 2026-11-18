"""模型路由与三态熔断。

为什么生产必须要有这一层：

    单模型直连 = 单点故障。模型侧 429、超时、区域性故障，都会直接变成用户可见的失败。
    而重试只会放大拥堵（惊群）。

    正确做法是两层配合：
        熔断 —— 某家已经挂了，就别再打它，快速失败并给它恢复时间
        路由 —— 换一家更便宜的/更稳的/同区域的顶上

对应面试考点：
    - 模型路由策略（成本优先 / 质量优先 / 延迟优先）
    - 三态熔断器（Closed / Open / Half-Open）
    - 重试与熔断的分工
    - 自动降级
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pdc.llm import LLM, LLMReply


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class ModelProfile:
    """一个可路由的模型。价格单位是美元 / 百万 token。"""

    name: str
    input_price: float
    output_price: float
    context: int
    supports_tools: bool = True
    supports_json: bool = True
    tier: int = 2  # 1 便宜、2 中、3 强


# 只放公开可查的价格，避免写出内部价格造成误导。
# 实际使用用 PDC_MODELS 覆盖。
DEFAULT_CATALOG: dict[str, ModelProfile] = {
    "deepseek-flash": ModelProfile(
        name="deepseek-flash", input_price=0.30, output_price=1.20,
        context=1_000_000, tier=1,
    ),
    "deepseek-v4-pro": ModelProfile(
        name="deepseek-v4-pro", input_price=1.32, output_price=3.96,
        context=1_000_000, tier=3,
    ),
}


def load_catalog(raw: str | None = None) -> dict[str, ModelProfile]:
    """PDC_MODELS="name:in:out:ctx:tier,name2:..."，不设就用内置表。"""
    if not raw:
        return dict(DEFAULT_CATALOG)
    out: dict[str, ModelProfile] = {}
    for item in raw.split(","):
        parts = item.strip().split(":")
        if len(parts) < 2:
            continue
        name = parts[0]
        out[name] = ModelProfile(
            name=name,
            input_price=float(parts[1]),
            output_price=float(parts[2]) if len(parts) > 2 else 0.0,
            context=int(parts[3]) if len(parts) > 3 else 128_000,
            tier=int(parts[4]) if len(parts) > 4 else 2,
        )
    return out or dict(DEFAULT_CATALOG)


@dataclass
class CircuitBreaker:
    """三态熔断器。

    CLOSED  正常放行，统计失败
    OPEN    直接拒绝，等待冷却
    HALF_OPEN 冷却结束，放少量探测，成功则恢复，失败则再打开
    """

    name: str
    failure_threshold: int = 5
    success_threshold: int = 2
    open_seconds: float = 30.0
    half_open_max_calls: int = 2

    state: State = State.CLOSED
    failures: int = 0
    successes_half: int = 0
    half_open_inflight: int = 0
    open_until: float = 0.0
    trips: int = 0

    def _now(self) -> float:
        return time.monotonic()

    def allow(self) -> bool:
        now = self._now()
        if self.state is State.OPEN:
            if now < self.open_until:
                return False
            self.state = State.HALF_OPEN
            self.successes_half = 0
            self.half_open_inflight = 0
        if self.state is State.HALF_OPEN:
            return self.half_open_inflight < self.half_open_max_calls
        return True

    def before_call(self) -> None:
        if self.state is State.HALF_OPEN:
            self.half_open_inflight += 1

    def on_success(self) -> None:
        if self.state is State.HALF_OPEN:
            self.successes_half += 1
            self.half_open_inflight = max(0, self.half_open_inflight - 1)
            if self.successes_half >= self.success_threshold:
                self.state = State.CLOSED
                self.failures = 0
                self.successes_half = 0
        else:
            self.failures = 0

    def on_failure(self) -> None:
        if self.state is State.HALF_OPEN:
            self._trip()
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self.state = State.OPEN
        self.open_until = self._now() + self.open_seconds
        self.failures = 0
        self.successes_half = 0
        self.half_open_inflight = 0
        self.trips += 1

    def snapshot(self) -> dict:
        return {
            "model": self.name,
            "state": self.state.value,
            "failures": self.failures,
            "trips": self.trips,
        }


class Router:
    """按约束挑候选模型，按顺序尝试。

    prefer:
        cost      —— 便宜的优先（批处理）
        quality   —— 强的优先（复杂推理）
        balanced  —— 默认，按 tier 升序
    """

    def __init__(self, catalog: dict[str, ModelProfile], prefer: str = "balanced") -> None:
        if not catalog:
            raise ValueError("模型目录不能为空")
        self.catalog = catalog
        self.prefer = prefer

    def candidates(
        self,
        *,
        needs_tools: bool = True,
        needs_json: bool = False,
        est_tokens: int = 0,
    ) -> list[ModelProfile]:
        usable = [
            m for m in self.catalog.values()
            if (not needs_tools or m.supports_tools)
            and (not needs_json or m.supports_json)
            and m.context >= est_tokens
        ]
        if not usable:
            return []

        if self.prefer == "cost":
            key: Callable[[ModelProfile], Any] = lambda m: (m.input_price, m.tier)
        elif self.prefer == "quality":
            key = lambda m: (-m.tier, m.input_price)
        else:
            key = lambda m: (m.tier, m.input_price)
        return sorted(usable, key=key)


class RoutedLLM:
    """把路由和熔断包成一个 LLM，对上层完全透明。

    上层（agent/audit）不需要知道背后有几个模型、哪个挂了。
    """

    def __init__(
        self,
        router: Router,
        factory: Callable[[str], LLM],
        *,
        breaker_config: dict | None = None,
    ) -> None:
        self.router = router
        self.factory = factory
        self._clients: dict[str, LLM] = {}
        cfg = breaker_config or {}
        self.breakers = {
            name: CircuitBreaker(name=name, **cfg) for name in router.catalog
        }
        self.name = "routed(" + ",".join(router.catalog) + ")"
        # 供 trace / 汇报使用
        self.last_route: dict | None = None
        self.route_history: list[dict] = []

    def _client(self, name: str) -> LLM:
        if name not in self._clients:
            self._clients[name] = self.factory(name)
        return self._clients[name]

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        est = sum(len(str(m.get("content", ""))) for m in messages) // 3
        candidates = self.router.candidates(
            needs_tools=bool(tools), needs_json=False, est_tokens=est
        )
        if not candidates:
            raise RuntimeError("没有满足约束的模型")

        errors: list[str] = []
        for profile in candidates:
            breaker = self.breakers[profile.name]
            if not breaker.allow():
                errors.append(f"{profile.name}: 熔断中({breaker.state.value})")
                continue

            breaker.before_call()
            try:
                reply = self._client(profile.name).chat(messages, tools)
                breaker.on_success()
                self._record(profile, breaker, ok=True)
                return reply
            except Exception as exc:  # noqa: BLE001
                breaker.on_failure()
                errors.append(f"{profile.name}: {type(exc).__name__}: {exc}")
                self._record(profile, breaker, ok=False)
                continue

        raise RuntimeError("所有候选模型均不可用 → " + " | ".join(errors))

    def _record(self, profile: ModelProfile, breaker: CircuitBreaker, ok: bool) -> None:
        self.last_route = {
            "model": profile.name,
            "tier": profile.tier,
            "ok": ok,
            "breaker": breaker.state.value,
        }
        self.route_history.append(self.last_route)

    def snapshot(self) -> dict:
        return {
            "prefer": self.router.prefer,
            "breakers": [b.snapshot() for b in self.breakers.values()],
            "routes": len(self.route_history),
            "fallbacks": sum(1 for r in self.route_history if not r["ok"]),
        }
