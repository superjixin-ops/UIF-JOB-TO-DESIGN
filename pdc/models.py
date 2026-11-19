"""结构化表示：把 pipeline YAML 变成可复现的事实。

设计原则：frozen dataclass（不可变），因为
1. 可以被缓存、被多个工具安全共享
2. 不会出现某个工具偷偷改掉字段、导致后面全错
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str  # IntegerType / StringType / DecimalType / TimestampType ...
    nullable: bool
    precision: int | None = None
    scale: int | None = None


@dataclass(frozen=True)
class ExtractSpec:
    alias: str
    connector: str
    credentials: str
    query: str
    main_df: bool = False
    natural_key: tuple[str, ...] = ()
    # 连接级参数。这些字段原来不进文档，导致"文档 -> YAML"会静默丢掉它们。
    # 用 tuple-of-pairs 而不是 dict：dataclass 是 frozen 的，dict 会让它不可哈希。
    extract_type: str = "default"
    conn_alias: str = ""
    key_name: str = ""
    flags: tuple[tuple[str, Any], ...] = ()
    template_source: str = ""


@dataclass(frozen=True)
class TransformSpec:
    index: int
    transform_type: str
    inputs: tuple[str, ...] = ()
    output: str | None = None
    sql: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LoadObject:
    """一个 load connection。一个 job 可以有多个（同时写多张表）。"""

    object_name: str
    query: str
    mode: str = ""
    partitions: int | None = None
    partition_by: tuple[str, ...] = ()
    # 原始 loadObject。和 metadata.name 是两回事，混在一起模型只能猜。
    load_object: str = ""


@dataclass(frozen=True)
class LoadSpec:
    object_name: str
    connector: str
    mode: str
    query: str
    partitions: int | None
    schema: tuple[Column, ...] = ()
    partition_by: tuple[str, ...] = ()
    objects: tuple[LoadObject, ...] = ()
    credentials: str = ""
    conn_alias: str = ""
    table_space: str = ""
    flip_alias: bool = False


@dataclass(frozen=True)
class JobSpec:
    job_type: str
    extracts: tuple[ExtractSpec, ...] = ()
    transforms: tuple[TransformSpec, ...] = ()
    load: LoadSpec | None = None
    size: str = "?"


@dataclass(frozen=True)
class PipelineFacts:
    name: str
    kind: str
    owner: str
    jobs: tuple[JobSpec, ...]
    source_path: str

    # ---------- 派生事实：这些是"分析结论"，不是原始数据 ----------

    def job(self, job_type: str) -> JobSpec | None:
        for j in self.jobs:
            if j.job_type == job_type:
                return j
        return None

    def summary(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "owner": self.owner,
            "jobs": [
                {
                    "job_type": j.job_type,
                    "size": j.size,
                    "extracts": len(j.extracts),
                    "transforms": len(j.transforms),
                    "load_columns": len(j.load.schema) if j.load else 0,
                }
                for j in self.jobs
            ],
        }
