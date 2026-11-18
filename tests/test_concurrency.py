from __future__ import annotations

import asyncio
import time

import pytest

from pdc.concurrency import (
    BoundedRunner,
    BusyError,
    Metrics,
    closed_loop,
    percentile,
)


# ---------------------------------------------------------------- 分位数
def test_percentile_basic():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile([], 95) == 0
    assert percentile([7], 99) == 7


def test_metrics_summary():
    m = Metrics()
    for ms in (100, 200, 300, 400, 500):
        m.add(ms, True)
    m.add(900, False, "boom")
    s = m.summary()
    assert s["count"] == 6
    assert s["errors"] == 1
    assert s["error_rate"] == pytest.approx(0.167, abs=0.001)
    assert s["p50_ms"] == 350  # 6 个值的中位数是 300 和 400 的插值
    assert s["max_ms"] == 900


# ---------------------------------------------------------------- 并发上限
def test_runner_never_exceeds_limit():
    """关键断言：任意时刻 in_flight 不超过 limit。"""
    limit = 3
    peak = {"value": 0}
    lock = asyncio.Lock()

    async def scenario():
        runner = BoundedRunner(limit=limit, on_full="wait")

        def slow(_i: int) -> None:
            time.sleep(0.05)

        async def one(i: int) -> None:
            async with lock:
                peak["value"] = max(peak["value"], runner.in_flight + 1)
            await runner.run(slow, i)

        metrics = Metrics()
        await closed_loop(one, 12, workers=12, metrics=metrics)
        return metrics

    metrics = asyncio.run(scenario())
    assert peak["value"] <= limit + 1  # +1 是测量时机误差（进入前读）
    assert metrics.summary()["count"] == 12
    assert metrics.summary()["errors"] == 0


def test_runner_wait_mode_queues_everything():
    async def scenario():
        runner = BoundedRunner(limit=2, on_full="wait")

        def work(_i: int) -> int:
            time.sleep(0.02)
            return 1

        async def one(i: int) -> None:
            await runner.run(work, i)

        metrics = Metrics()
        await closed_loop(one, 10, workers=10, metrics=metrics)
        return metrics, runner

    metrics, runner = asyncio.run(scenario())
    assert metrics.summary()["errors"] == 0
    assert runner.in_flight == 0


def test_runner_reject_mode_fails_fast():
    async def scenario():
        runner = BoundedRunner(limit=2, on_full="reject")

        def work(_i: int) -> None:
            time.sleep(0.05)

        async def one(i: int) -> None:
            await runner.run(work, i)

        metrics = Metrics()
        await closed_loop(one, 12, workers=12, metrics=metrics)
        return metrics

    metrics = asyncio.run(scenario())
    s = metrics.summary()
    assert s["errors"] > 0, "reject 模式必须拒绝一部分"
    assert s["ok"] >= 1, "不能全拒（说明准入控制实现有误）"
    assert s["error_rate"] < 1.0


def test_busy_error_message():
    """占满后再申请必须抛 BusyError。

    注意：不能 await run() 之后再申请，因为 await 结束时槽位已释放。
    要用 create_task 把槽位占着。
    """

    async def scenario():
        runner = BoundedRunner(limit=1, on_full="reject")

        def work() -> None:
            time.sleep(0.2)

        holder = asyncio.create_task(runner.run(work))
        await asyncio.sleep(0.05)          # 等它进到槽位里
        try:
            with pytest.raises(BusyError):
                await runner._enter()      # noqa: SLF001 - 直接测准入
        finally:
            await holder

    asyncio.run(scenario())


def test_invalid_args():
    with pytest.raises(ValueError):
        BoundedRunner(limit=0)
    with pytest.raises(ValueError):
        BoundedRunner(limit=1, on_full="whatever")


# ---------------------------------------------------------------- 稳定性
def test_many_short_tasks_do_not_deadlock():
    async def scenario():
        runner = BoundedRunner(limit=4, on_full="wait")

        def work(i: int) -> int:
            return i

        async def one(i: int) -> None:
            await runner.run(work, i)

        metrics = Metrics()
        await closed_loop(one, 200, workers=16, metrics=metrics)
        return metrics

    s = asyncio.run(scenario()).summary()
    assert s["count"] == 200
    assert s["errors"] == 0
