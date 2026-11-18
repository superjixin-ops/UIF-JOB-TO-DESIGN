"""确定性检查。这些函数替代"让模型自己数数"。

核心思想：凡是精确计算（列数、差集、编号），都必须用代码算。
模型只负责"这个差异意味着什么"，不负责"差在哪"。
"""

from __future__ import annotations

import re

from pdc.models import JobSpec, PipelineFacts, TransformSpec


def apply_transform(frame: list[str], t: TransformSpec) -> list[str]:
    """按一个 transform 对"当前列清单"做一次模拟。

    为什么不能只看 Drop：
        Select 也会删列（保留清单之外的都消失）。
        如果只统计 Drop，一个用 Select 收窄的 job 会被误报成"有列未声明"。
        这是替换样例数据时才暴露出来的真实缺陷。

    这是有序的：transforms 按执行顺序作用在列清单上。
    """
    if t.transform_type == "Drop":
        gone = {str(c) for c in (t.detail.get("columns") or ())}
        return [c for c in frame if c not in gone]

    if t.transform_type == "RenameColumns":
        mapping = {str(k): str(v) for k, v in (t.detail.get("mapColumns") or {}).items()}
        return [mapping.get(c, c) for c in frame]

    if t.transform_type == "Select":
        keep = [str(c) for c in (t.detail.get("columns") or ())]
        if not keep:
            return frame
        present = [c for c in keep if c in frame]
        # Select 里可能有上游新增的列（AddColumns 产出的），也要保留
        fresh = [c for c in keep if c not in frame]
        return present + fresh

    return frame


def simulate_frame(incoming: list[str], transforms: tuple[TransformSpec, ...]) -> list[str]:
    """把下游 job 的 transform 链跑一遍，得到它实际写出的列清单。"""
    frame = list(incoming)
    for t in transforms:
        frame = apply_transform(frame, t)
    return frame


def dropped_and_renamed(job: JobSpec) -> tuple[set[str], dict[str, str]]:
    """只统计 Drop 和 Rename 的净效果（保留给需要粗粒度视图的调用方）。"""
    dropped: set[str] = set()
    renamed: dict[str, str] = {}

    for t in job.transforms:
        if t.transform_type == "Drop":
            dropped.update(str(c) for c in (t.detail.get("columns") or ()))
        elif t.transform_type == "RenameColumns":
            mapping = t.detail.get("mapColumns") or {}
            renamed.update({str(k): str(v) for k, v in mapping.items()})

    return dropped, renamed


def schema_diff(facts: PipelineFacts, job_index: int = 0) -> list[str]:
    """比对上游 job 产出的数据帧列 vs 下游 load schema 声明的列。

    典型用途：EID job 写 N 列，datamart job 只 Drop 一部分、Select 一部分，
    但 load.schema 声明的列数与实际写出不一致 —— 中间那些列既没被删也没被声明。
    """
    jobs = facts.jobs
    if job_index + 1 >= len(jobs):
        return []

    src, dst = jobs[job_index], jobs[job_index + 1]
    if not src.load or not dst.load:
        return []

    # 下游没有声明 schema（例如 jdbc 直写，交给 connector 建表）时无法比对。
    # 明确说出来，不要假装"没有差异"。
    if not dst.load.schema:
        return [f"[schema_undeclared] {dst.job_type} 没有 schema 声明，跳过列比对"]

    incoming = [c.name for c in src.load.schema]
    frame = simulate_frame(incoming, dst.transforms)
    declared = [c.name for c in dst.load.schema]
    declared_set = set(declared)

    issues: list[str] = []

    extra = [c for c in frame if c not in declared_set]
    if extra:
        issues.append(
            f"[schema_mismatch] {len(extra)} 列在数据帧里但 schema 未声明: {extra}"
        )

    frame_set = set(frame)
    missing = [c for c in declared if c not in frame_set]
    if missing:
        issues.append(
            f"[schema_missing] {len(missing)} 列在 schema 里但数据帧没有: {missing}"
        )

    return issues


HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")


def check_numbering(lines: list[str]) -> list[str]:
    """检查章节编号是否连续（同层级不跳号）。"""
    issues: list[str] = []
    last_major: int | None = None

    for line in lines:
        m = HEADING_RE.match(line.strip())
        if not m:
            continue
        parts = m.group(1).split(".")
        major = int(parts[0])
        if last_major is not None and major not in (last_major, last_major + 1):
            issues.append(f"[numbering] 跳号: {m.group(1)}（上一个是 {last_major}）")
        last_major = major

    return issues
