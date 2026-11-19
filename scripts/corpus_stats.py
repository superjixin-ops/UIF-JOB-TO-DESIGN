"""语料结构统计：先看分布，再决定要不要做检索。

为什么先做这一步：

    在 5,000+ 条 pipeline 上做向量检索之前，必须先回答一个问题——
    **这些 pipeline 到底有多少种"形状"？**

    如果 90% 的文件都属于十来种结构模板，那"找相似 pipeline"不需要检索，
    一个分类器（甚至一张规则表）就够了。反过来，如果形状极其分散，
    检索才有意义，而且"相似"的定义必须先想清楚。

用法：

    uv run python -m scripts.corpus_stats --repo ../CRD-7/pipelines
    uv run python -m scripts.corpus_stats --repo samples/pipelines --json out/stats.json

只读：不修改被扫描的目录，输出写到 --json 指定的位置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdc.parser import PipelineParseError, parse_pipeline


def _signature(facts) -> tuple:
    """结构指纹：只保留"形状"，不保留具体名字。

    这是判断"有多少种模板"的核心。故意不含 metadata.name / owner，
    因为它们不影响结构。
    """
    chain = tuple(j.job_type for j in facts.jobs)
    connectors = tuple(sorted({e.connector for j in facts.jobs for e in j.extracts}))
    transforms = tuple(
        sorted({t.transform_type for j in facts.jobs for t in j.transforms})
    )
    has_schema = any(j.load and j.load.schema for j in facts.jobs)
    return (facts.kind, chain, connectors, transforms, has_schema)


def scan(root: Path, limit: int | None = None) -> dict:
    files = sorted(p for p in root.rglob("*.yaml") if p.is_file())
    if limit:
        files = files[:limit]

    stats: dict = {
        "root": str(root),
        "files": len(files),
        "parsed": 0,
        "failed": 0,
        "failures": Counter(),
        "kinds": Counter(),
        "chains": Counter(),
        "signatures": Counter(),
        "connectors": Counter(),
        "load_connectors": Counter(),
        "sizes": Counter(),
        "transforms": Counter(),
        "columns": Counter(),
        "jobs_per_pipeline": Counter(),
        "columns_per_load": Counter(),
        "lines_per_file": Counter(),
        "main_df_anomalies": 0,
        "no_schema_pipelines": 0,
        "partitioned_loads": 0,
        "loads": 0,
        "duplicate_hashes": 0,
        "total_lines": 0,
    }
    hashes: set[str] = set()

    for path in files:
        try:
            facts = parse_pipeline(path)
        except (PipelineParseError, OSError, UnicodeDecodeError) as exc:
            stats["failed"] += 1
            stats["failures"][type(exc).__name__] += 1
            continue

        stats["parsed"] += 1
        stats["kinds"][facts.kind] += 1
        stats["jobs_per_pipeline"][len(facts.jobs)] += 1
        stats["chains"][tuple(j.job_type for j in facts.jobs)] += 1
        stats["signatures"][_signature(facts)] += 1

        for job in facts.jobs:
            stats["sizes"][job.size] += 1
            mains = sum(1 for e in job.extracts if e.main_df)
            if job.extracts and mains != 1:
                stats["main_df_anomalies"] += 1
            for e in job.extracts:
                stats["connectors"][e.connector] += 1
            for t in job.transforms:
                stats["transforms"][t.transform_type] += 1
            if job.load:
                stats["loads"] += 1
                stats["load_connectors"][job.load.connector] += 1
                if job.load.partition_by:
                    stats["partitioned_loads"] += 1
                stats["columns_per_load"][len(job.load.schema)] += 1
                for c in job.load.schema:
                    stats["columns"][c.name] += 1

        if not any(j.load and j.load.schema for j in facts.jobs):
            stats["no_schema_pipelines"] += 1

        # 完全重复的文件（内容一致）
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in hashes:
            stats["duplicate_hashes"] += 1
        else:
            hashes.add(digest)

        try:
            n_lines = len(path.read_text(encoding="utf-8").splitlines())
            stats["total_lines"] += n_lines
            bucket = 100 if n_lines < 100 else (300 if n_lines < 300 else (600 if n_lines < 600 else 1000))
            stats["lines_per_file"][f"{bucket}+"] += 1
        except (OSError, UnicodeDecodeError):
            pass

    return stats


def _coverage(counter: Counter, top: int) -> float:
    total = sum(counter.values()) or 1
    return sum(n for _, n in counter.most_common(top)) / total


def report(stats: dict) -> None:
    print(f"扫描: {stats['root']}")
    print(f"文件: {stats['files']}  解析成功: {stats['parsed']}  失败: {stats['failed']}")
    if stats["failed"]:
        print(f"  失败类型: {dict(stats['failures'])}")
    print(f"总行数: {stats['total_lines']:,}  平均: {stats['total_lines'] // max(stats['parsed'], 1)} 行/文件")
    print(f"完全重复的文件: {stats['duplicate_hashes']}")
    print()

    print("== 种类 ==")
    for k, v in stats["kinds"].most_common():
        print(f"  {k:22} {v:6}  {v / max(stats['parsed'], 1):.1%}")
    print()

    print(f"== job 链 Top 15（共 {len(stats['chains'])} 种）==")
    for chain, n in stats["chains"].most_common(15):
        print(f"  {n:6}  {' -> '.join(chain)}")
    print(f"  Top 5 覆盖: {_coverage(stats['chains'], 5):.1%}"
          f"   Top 10 覆盖: {_coverage(stats['chains'], 10):.1%}")
    print()

    print(f"== 结构指纹 Top 10（共 {len(stats['signatures'])} 种）==")
    for sig, n in stats["signatures"].most_common(10):
        kind, chain, conns, txs, schema = sig
        print(f"  {n:6}  {kind} | {'/'.join(chain)} | {','.join(conns)}"
              f" | {len(txs)} 种变换 | schema={'Y' if schema else 'N'}")
    print(f"  Top 10 覆盖: {_coverage(stats['signatures'], 10):.1%}"
          f"   Top 50 覆盖: {_coverage(stats['signatures'], 50):.1%}")
    print()

    print("== 抽取连接器 ==")
    for k, v in stats["connectors"].most_common(10):
        print(f"  {k:22} {v}")
    print("== 写入连接器 ==")
    for k, v in stats["load_connectors"].most_common(10):
        print(f"  {k:22} {v}")
    print()

    print("== 规模档位 ==")
    for k, v in stats["sizes"].most_common():
        print(f"  {k:6} {v}")
    print()

    print(f"== 变换类型 Top 15（共 {len(stats['transforms'])} 种）==")
    for k, v in stats["transforms"].most_common(15):
        print(f"  {k:28} {v}")
    print()

    print(f"== 列名 Top 30（共 {len(stats['columns'])} 个不同列名）==")
    for k, v in stats["columns"].most_common(30):
        print(f"  {k:40} {v}")
    print(f"  列名出现 1 次的: {sum(1 for n in stats['columns'].values() if n == 1)}")
    print()

    print("== 健康度 ==")
    parsed = max(stats["parsed"], 1)
    print(f"  没有声明 schema 的 pipeline : {stats['no_schema_pipelines']:6}  {stats['no_schema_pipelines'] / parsed:.1%}")
    print(f"  load 带分区             : {stats['partitioned_loads']:6} / {stats['loads']}")
    print(f"  main_df 异常（0 或 >1） : {stats['main_df_anomalies']:6} 个 job")
    print(f"  每文件 job 数分布       : {dict(sorted(stats['jobs_per_pipeline'].items()))}")


def to_json(stats: dict) -> dict:
    out = {}
    for k, v in stats.items():
        if isinstance(v, Counter):
            if v and isinstance(next(iter(v)), tuple):
                out[k] = [{"key": list(key), "count": n} for key, n in v.most_common()]
            else:
                out[k] = dict(v.most_common())
        else:
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="语料结构统计（只读）")
    ap.add_argument("--repo", default="samples/pipelines", help="要扫描的目录")
    ap.add_argument("--json", default="", help="把完整统计写到这个文件")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 个文件（调试用）")
    args = ap.parse_args(argv)

    root = Path(args.repo)
    if not root.exists():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 2

    stats = scan(root, limit=args.limit or None)
    report(stats)

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(to_json(stats), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n完整统计: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
