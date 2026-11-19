"""逐键保真审计：拿产物和源文件比，不看任何指标。

为什么需要这个工具，而不是加一条校验规则：

    校验规则只能检查你想到的东西。
    这个工具检查的是**你没想到的东西**——它不需要知道哪些字段重要，
    它把两个 YAML 展开成同样的扁平键值对，直接比。

第一次用它，就是在"校验器报 1.0"的产物上跑出来的：
50 个连接级键值里丢了 14 个，而所有指标都是满分。

用法：

    uv run python -m eval.fidelity 源.yaml 产物.yaml
    uv run python -m eval.fidelity 源.yaml 产物.yaml --json

退出码：一致 0，有差异 1。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# 只比"语义字段"。注释、空行、键序不属于语义，比它们只会制造噪音。
IGNORED_KEYS = {"documentation", "comment", "description"}


@dataclass
class Diff:
    key: tuple
    expected: Any
    actual: Any

    def __str__(self) -> str:
        path = ".".join(str(x) for x in self.key if x != "")
        return f"{path}: 源={self.expected!r} 产物={self.actual!r}"


def flatten(doc: dict) -> dict[tuple, Any]:
    """把 pipeline YAML 展开成 (job序号, 层级, 名字, 键) -> 值。

    只展开有语义的位置：job 的 size / extract 连接 / transform / load 连接 /
    load schema 列。其它字段（apiVersion、owner）也带上，但放在根层级。
    """
    out: dict[tuple, Any] = {}
    spec = doc.get("spec") or {}
    out[("spec", "owner", "", "name")] = str((spec.get("owner") or {}).get("name", ""))

    for i, job in enumerate(spec.get("jobs") or []):
        out[(i, "job", "", "jobType")] = str(job.get("jobType", ""))
        out[(i, "job", "", "size")] = str((job.get("setups") or {}).get("size", ""))

        for e in job.get("extract") or []:
            alias = str(e.get("alias") or e.get("templateSource") or "")
            out[(i, "extract", alias, "main_df")] = bool(e.get("main_df", False))
            for c in e.get("connections") or []:
                for k, v in c.items():
                    if k in IGNORED_KEYS:
                        continue
                    out[(i, "extract", alias, str(k))] = _norm(v)

        for t in job.get("transform") or []:
            ttype = str(t.get("transformType", ""))
            out[(i, "transform", ttype, "inputs")] = _norm(t.get("objectNames"))
            out[(i, "transform", ttype, "output")] = _norm(t.get("aliasResult"))
            for k, v in t.items():
                if k in ("transformType", "objectNames", "aliasResult", "query"):
                    continue
                if k in IGNORED_KEYS:
                    continue
                out[(i, "transform", ttype, str(k))] = _norm(v)
            if t.get("query") and ttype == "SparkSql":
                out[(i, "transform", ttype, "query")] = _norm(t["query"])

        load = job.get("load") or {}
        meta = (load.get("metadata") or {}).get("name")
        if meta:
            out[(i, "load", "", "metadata.name")] = str(meta)
        for c in load.get("connections") or []:
            for k, v in c.items():
                if k in IGNORED_KEYS:
                    continue
                out[(i, "load", "", str(k))] = _norm(v)
        for col in load.get("schema") or []:
            name = str(col.get("columnName", ""))
            out[(i, "column", name, "dataType")] = _norm(col.get("dataType"))
            out[(i, "column", name, "nullAble")] = _norm(col.get("nullAble"))
            out[(i, "column", name, "dataTypeArgs")] = _norm(col.get("dataTypeArgs"))
    return out


def _norm(value: Any) -> Any:
    """归一化：数字与字符串、dict 键序、list 顺序都不该造成假阳性。"""
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    return str(value).strip()


def compare(source: dict, produced: dict) -> list[Diff]:
    a, b = flatten(source), flatten(produced)
    diffs = [Diff(k, a[k], b.get(k)) for k in a if a[k] != b.get(k)]
    # 产物多出来的键也报——编造字段和漏字段一样是失真
    diffs += [Diff(k, None, b[k]) for k in b if k not in a]
    return sorted(diffs, key=lambda d: str(d.key))


def audit(source_path: str | Path, produced_path: str | Path) -> list[Diff]:
    src = yaml.safe_load(Path(source_path).read_text(encoding="utf-8"))
    prod = yaml.safe_load(Path(produced_path).read_text(encoding="utf-8"))
    return compare(src, prod)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="fidelity", description="逐键比对两份 pipeline YAML")
    ap.add_argument("source", help="基准文件（设计文档的来源）")
    ap.add_argument("produced", help="待检查的产物")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    diffs = audit(args.source, args.produced)
    total = len(flatten(yaml.safe_load(Path(args.source).read_text(encoding="utf-8"))))

    if args.json:
        print(json.dumps(
            {"total_keys": total, "diff_count": len(diffs),
             "diffs": [{"key": list(d.key), "expected": d.expected, "actual": d.actual} for d in diffs]},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(f"源 {total} 个键值，缺/不同 {len(diffs)} 个")
        for d in diffs:
            print(f"  {d}")

    if diffs:
        print("\n注意：这些差异不等于模型错了。先分清是「文档没写」还是「模型没写」。")
        print("判据见 设计转YAML-Agent-幻觉与测试治理.md 第 3.15 节的三步定位法。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
