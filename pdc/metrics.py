"""指标：把"服务现在怎么样"变成可以抓取的数字。

为什么手写而不用 prometheus-client：

    只需要 4 种原语（counter / gauge / histogram / 文本格式），
    手写 100 行就够，而且格式完全可控。
    多一个依赖意味着 Dockerfile、lock 文件、升级都要跟着动。

指标设计的三条规矩：

    1. **低基数**：path 用路由模板（/runs/{run_id}），不用真实 id，
       否则每个请求都产生一个新时间序列，Prometheus 会被打爆。
    2. **命名带单位**：duration_seconds、tokens_total。
    3. **只暴露聚合值**：绝不在指标里放 token、用户输入、文件路径。
"""

from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def _labels(labels: dict | None) -> str:
    if not labels:
        return ""
    body = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + body + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], float] = defaultdict(float)
        self._gauges: dict[tuple[str, str], float] = {}
        self._hist: dict[tuple[str, str], dict] = {}

    def inc(self, name: str, labels: dict | None = None, value: float = 1.0) -> None:
        with self._lock:
            self._counters[(name, _labels(labels))] += value

    def set(self, name: str, value: float, labels: dict | None = None) -> None:
        with self._lock:
            self._gauges[(name, _labels(labels))] = float(value)

    def observe(self, name: str, seconds: float, labels: dict | None = None) -> None:
        key = (name, _labels(labels))
        with self._lock:
            slot = self._hist.get(key)
            if slot is None:
                slot = {"buckets": [0] * len(BUCKETS), "sum": 0.0, "count": 0}
                self._hist[key] = slot
            slot["sum"] += seconds
            slot["count"] += 1
            for i, edge in enumerate(BUCKETS):
                if seconds <= edge:
                    slot["buckets"][i] += 1
                    break
            else:
                slot["buckets"][-1] += 1  # +Inf 桶由 count 兜底

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name}{labels} {value:g}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name}{labels} {value:g}")
            for (name, labels), slot in sorted(self._hist.items()):
                lines.append(f"# TYPE {name} histogram")
                # 内部存的是"落在本桶"的计数，输出要转成累计值——
                # Prometheus 的 _bucket 语义是"小于等于 le 的累计数"，
                # 直接输出非累计值会让分位数算错，而且错得很隐蔽。
                running = 0
                for i, edge in enumerate(BUCKETS):
                    running += slot["buckets"][i]
                    lines.append(f"{name}_bucket{_merge(labels, f'le=\"{edge:g}\"')} {running}")
                lines.append(f"{name}_bucket{_merge(labels, 'le=\"+Inf\"')} {slot['count']}")
                lines.append(f"{name}_sum{labels} {slot['sum']:.6f}")
                lines.append(f"{name}_count{labels} {slot['count']}")
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": {f"{n}{labels}": v for (n, labels), v in self._counters.items()},
                "gauges": {f"{n}{labels}": v for (n, labels), v in self._gauges.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist.clear()


def _merge(labels: str, extra: str) -> str:
    if not labels:
        return "{" + extra + "}"
    return labels[:-1] + "," + extra + "}"


METRICS = Metrics()


def percentile_from_hist(text: str, metric: str, p: float) -> float:
    """从文本格式里估算分位数（用于告警检查，不追求精确）。

    做法：按 le 分桶累加，找到第一个累计占比 >= p 的桶，返回它的上界。
    对于"延迟超过 3 秒就告警"这种判断，够用且稳定。
    """
    buckets: list[tuple[float, float]] = []
    for line in text.splitlines():
        if not line.startswith(f"{metric}_bucket"):
            continue
        labels, _, value = line.partition(" ")
        le = labels.split('le="')[1].split('"')[0]
        edge = float("inf") if le == "+Inf" else float(le)
        buckets.append((edge, float(value)))
    if not buckets:
        return 0.0
    total = buckets[-1][1]
    if total <= 0:
        return 0.0
    target = total * p
    for edge, count in buckets:
        if count >= target:
            return edge
    return buckets[-1][0]
