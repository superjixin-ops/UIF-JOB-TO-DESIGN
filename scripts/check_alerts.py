"""按 ops/alerts.yml 检查 /metrics，输出告警并设置退出码。

用法：
    python -m scripts.check_alerts --url http://127.0.0.1:8000/metrics --token xxx
    python -m scripts.check_alerts --file out/metrics.txt

退出码：0 无告警，1 有 ticket，2 有 page。
这样可以直接挂到 cron / k8s probe / CI 里。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from pdc.metrics import percentile_from_hist


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def _sum_metric(text: str, name: str, predicate=None) -> float:
    total = 0.0
    for line in _lines(text):
        if not line.startswith(name):
            continue
        if predicate and not predicate(line):
            continue
        total += float(line.rsplit(" ", 1)[1])
    return total


def evaluate(text: str, rules: list[dict]) -> list[dict]:
    out: list[dict] = []
    for rule in rules:
        kind = rule["kind"]
        fired = False
        actual = 0.0
        if kind == "error_rate":
            total = _sum_metric(text, "pdc_http_requests_total")
            bad = _sum_metric(text, "pdc_http_requests_total", lambda l: 'status="5' in l)
            actual = (bad / total) if total else 0.0
            fired = actual > rule["threshold"]
        elif kind == "p95_latency_ms":
            actual = percentile_from_hist(text, "pdc_http_request_duration_seconds", 0.95) * 1000
            fired = actual > rule["threshold"]
        elif kind == "counter":
            actual = _sum_metric(text, rule["metric"])
            fired = actual >= rule["threshold"]
        elif kind == "counter_below":
            actual = _sum_metric(text, rule["metric"])
            fired = actual < rule["threshold"]
        elif kind == "gauge_max":
            actual = max(
                [float(l.rsplit(" ", 1)[1]) for l in _lines(text) if l.startswith(rule["metric"])]
                or [0.0]
            )
            fired = actual > rule["threshold"]
        else:
            out.append({"name": rule["name"], "error": f"未知规则类型 {kind}"})
            continue
        if fired:
            out.append({**rule, "actual": round(actual, 4)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--rules", default="ops/alerts.yml")
    args = ap.parse_args(argv)

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.url:
        req = urllib.request.Request(args.url)
        if args.token:
            req.add_header("Authorization", f"Bearer {args.token}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
    else:
        print("需要 --url 或 --file", file=sys.stderr)
        return 2

    rules = yaml.safe_load(Path(args.rules).read_text(encoding="utf-8"))["rules"]
    alerts = evaluate(text, rules)
    if not alerts:
        print("OK 无告警")
        return 0
    print(json.dumps(alerts, ensure_ascii=False, indent=2))
    return 2 if any(a.get("severity") == "page" for a in alerts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
