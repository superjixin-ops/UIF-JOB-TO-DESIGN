"""并发控制与延迟指标。

为什么需要这一层：
  Agent 一次运行要 10+ 秒，中间 7~10 次模型调用、10+ 次工具调用。
  如果每个请求直接开线程，20 个并发就会同时向模型发 200 个请求 ——
  触发 rate limit、内存暴涨、p99 雪崩。

  正确做法是：有界的并发 + 排队策略 + 可观测的延迟分布。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Sample:
    latency_ms: int
    ok: bool
    error: str = ""


def percentile(values: list[int], p: float) -> int:
    """线性插值分位数。p 取 0~100。"""
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (p / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return int(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


@dataclass
class Metrics:
    samples: list[Sample] = field(default_factory=list)
    started_at: float = field(default_factory=time.perf_counter)

    def add(self, latency_ms: int, ok: bool = True, error: str = "") -> None:
        self.samples.append(Sample(latency_ms, ok, error))

    def summary(self) -> dict:
        lat = [s.latency_ms for s in self.samples]
        elapsed = max(time.perf_counter() - self.started_at, 1e-9)
        return {
            "count": len(self.samples),
            "ok": sum(1 for s in self.samples if s.ok),
            "errors": sum(1 for s in self.samples if not s.ok),
            "error_rate": round(sum(1 for s in self.samples if not s.ok) / max(len(self.samples), 1), 3),
            "p50_ms": percentile(lat, 50),
            "p95_ms": percentile(lat, 95),
            "p99_ms": percentile(lat, 99),
            "max_ms": max(lat) if lat else 0,
            "throughput_rps": round(len(self.samples) / elapsed, 2),
        }


class BoundedRunner:
    """有界并发执行器。

    on_full 决定超出容量时的行为：
      wait    —— 排队等待（默认）。适合后台任务。
      reject  —— 立刻抛 BusyError。适合在线请求，让调用方重试或降级。

    实现说明：不要用 Semaphore + wait_for(timeout=0) 做"非阻塞获取"，
    wait_for 在 timeout<=0 时会立刻取消，导致 100% 拒绝。
    用 Condition + 计数器，两种策略都正确。
    """

    def __init__(self, limit: int = 4, on_full: str = "wait") -> None:
        if limit < 1:
            raise ValueError("limit 必须 >= 1")
        if on_full not in ("wait", "reject"):
            raise ValueError("on_full 只能是 wait 或 reject")
        self.limit = limit
        self.on_full = on_full
        self._cond = asyncio.Condition()
        self._active = 0

    @property
    def in_flight(self) -> int:
        return self._active

    async def _enter(self) -> None:
        async with self._cond:
            if self.on_full == "reject":
                if self._active >= self.limit:
                    raise BusyError(f"服务繁忙，当前上限 {self.limit}")
                self._active += 1
                return
            while self._active >= self.limit:
                await self._cond.wait()
            self._active += 1

    async def _leave(self) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify(1)

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        await self._enter()
        try:
            # 同步 SDK（openai / openpyxl）放进线程，避免阻塞事件循环
            return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            await self._leave()


class BusyError(RuntimeError):
    """容量已满，快速失败。"""


async def gather_load(
    fn: Callable[[int], Awaitable[Any]],
    n: int,
    metrics: Metrics,
) -> None:
    """并发发起 n 个任务，记录每个的延迟。异常不中断整体。"""

    async def one(i: int) -> None:
        start = time.perf_counter()
        try:
            await fn(i)
            metrics.add(int((time.perf_counter() - start) * 1000), True)
        except Exception as exc:  # noqa: BLE001 - 压测就是要统计所有失败
            metrics.add(int((time.perf_counter() - start) * 1000), False, f"{type(exc).__name__}: {exc}")

    await asyncio.gather(*(one(i) for i in range(n)))


async def closed_loop(
    fn: Callable[[int], Awaitable[Any]],
    n_requests: int,
    workers: int,
    metrics: Metrics,
) -> None:
    """闭环压测：固定 workers 个并发，从队列里不断取任务，直到做完 n_requests 个。

    和 gather_load 的区别：
      gather_load  —— 一次性发起 N 个，测的是"瞬时打满"
      closed_loop  —— 固定并发数持续施压，测的是"稳态吞吐"
    后者才是 Little's Law（吞吐 = 并发 / 延迟）能对上的模型。
    """
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(n_requests):
        queue.put_nowait(i)

    async def worker() -> None:
        while True:
            try:
                i = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            start = time.perf_counter()
            try:
                await fn(i)
                metrics.add(int((time.perf_counter() - start) * 1000), True)
            except Exception as exc:  # noqa: BLE001
                metrics.add(
                    int((time.perf_counter() - start) * 1000), False,
                    f"{type(exc).__name__}: {exc}",
                )

    await asyncio.gather(*(worker() for _ in range(workers)))
