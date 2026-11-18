"""压测脚本。

两种模式：

  mock（默认）—— 用可控延迟模拟模型调用，零成本，可以扫并发梯度
  real        —— 真打 DeepSeek，验证端到端，按次计费（约 $0.014/次）

为什么要 mock：
  1. 真实模型有 rate limit，压不上去
  2. 要跑 1/4/8/16/32 五档，真跑 61 次约 $0.85，没必要
  3. 并发问题的根因在我们的排队策略，不在模型

真实链路已经单独验证过 3 次，mock 只用来测我们的调度层。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.concurrency import BoundedRunner, Metrics, closed_loop  # noqa: E402


def _mock_agent(latency_ms: int, jitter: float, failure_rate: float):
    """模拟一次 Agent 运行：模型延迟 + 工具延迟 + 偶发失败。"""

    def run(_i: int) -> dict:
        total = latency_ms * (1 + random.uniform(-jitter, jitter))
        time.sleep(total / 1000)
        if random.random() < failure_rate:
            raise RuntimeError("模拟上游 429")
        return {"ok": True}

    return run


async def sweep(
    concurrency_levels: list[int],
    n_requests: int,
    latency_ms: int,
    limit: int,
    on_full: str,
    failure_rate: float,
) -> list[dict]:
    results = []
    for level in concurrency_levels:
        metrics = Metrics()
        runner = BoundedRunner(limit=limit, on_full=on_full)
        fn = _mock_agent(latency_ms, 0.1, failure_rate)

        async def one(i: int, _level: int = level) -> None:
            await runner.run(fn, i)

        await closed_loop(one, n_requests, level, metrics)
        summary = metrics.summary()
        summary["client_concurrency"] = level
        summary["limit"] = limit
        summary["on_full"] = on_full
        results.append(summary)
        print(
            f"  客户端并发 {level:>3} | 服务端上限 {limit:>3} | p50 {summary['p50_ms']:>6}ms "
            f"p95 {summary['p95_ms']:>6}ms p99 {summary['p99_ms']:>6}ms "
            f"吞吐 {summary['throughput_rps']:>5}/s 错误 {summary['error_rate']:.0%}"
        )
    return results


async def real_run(n_requests: int, limit: int, pipeline: str) -> dict:
    """真打模型。用来验证端到端，不是用来压极限。"""
    from pdc.agent import run_agent
    from pdc.llm import OpenAICompatibleLLM
    from pdc.tools import Toolbox
    from pdc.trace import Tracer

    runner = BoundedRunner(limit=limit, on_full="wait")
    metrics = Metrics()

    def one_run(_i: int) -> dict:
        llm = OpenAICompatibleLLM("deepseek-chat", max_retries=2)
        tb = Toolbox(out_dir=Path("out/loadtest"), tracer=Tracer())
        res = run_agent(llm, tb, f"为 {pipeline} 生成设计文档", max_steps=10)
        return {"steps": res.steps, "written": bool(res.written)}

    async def one(i: int) -> None:
        await runner.run(one_run, i)

    await closed_loop(one, n_requests, min(n_requests, limit), metrics)
    return metrics.summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="并发压测")
    ap.add_argument("--mode", choices=["mock", "real"], default="mock")
    ap.add_argument("--requests", type=int, default=16)
    ap.add_argument("--latency-ms", type=int, default=1200, help="模拟单次运行耗时")
    ap.add_argument("--limit", type=int, default=4, help="服务端并发上限")
    ap.add_argument("--on-full", choices=["wait", "reject"], default="wait")
    ap.add_argument("--fail-rate", type=float, default=0.0)
    ap.add_argument("--pipeline", default="fact-order-fulfillment.yaml")
    ap.add_argument("--out", default="out/loadtest")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"模式={args.mode} 请求={args.requests} 服务端上限={args.limit} 排队策略={args.on_full}")

    if args.mode == "real":
        s = asyncio.run(real_run(args.requests, args.limit, args.pipeline))
        print(json.dumps(s, ensure_ascii=False, indent=2))
        (out / "real.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    levels = [1, 4, 8, 16, 32]
    results = asyncio.run(
        sweep(levels, args.requests, args.latency_ms, args.limit, args.on_full, args.fail_rate)
    )
    (out / "sweep.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out / 'sweep.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
