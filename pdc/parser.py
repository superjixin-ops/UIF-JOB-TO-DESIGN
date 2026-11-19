"""YAML -> PipelineFacts。这一层完全不用 LLM。

确定性优先：解析结果必须可复现，同一个文件永远得到同样的 facts。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pdc.models import (
    Column,
    ExtractSpec,
    JobSpec,
    LoadObject,
    LoadSpec,
    PipelineFacts,
    TransformSpec,
)

KNOWN_KINDS = {"FactPipeline", "DimensionPipeline"}


class PipelineParseError(Exception):
    """输入不是合法的 pipeline 定义。"""


def _columns(raw_schema: object) -> tuple[Column, ...]:
    """解析 load.schema。

    真实仓库里有 1,522 个文件写成 `schema: Template`（引用模板，不是内联列表）。
    这种情况返回空元组，表示"未内联声明"，而不是当成错误崩掉。
    """
    if not isinstance(raw_schema, list):
        return ()
    out: list[Column] = []
    for c in raw_schema:
        if not isinstance(c, dict) or "columnName" not in c:
            continue
        args = c.get("dataTypeArgs") or {}
        out.append(
            Column(
                name=str(c["columnName"]),
                data_type=str(c.get("dataType", "?")),
                nullable=bool(c.get("nullAble", False)),
                precision=args.get("precision"),
                scale=args.get("scale"),
            )
        )
    return tuple(out)


def _extract(raw: dict, index: int = 0) -> ExtractSpec:
    """解析一个 extract。

    真实仓库里有 extract 不写 alias，而是写 templateSource（引用模板生成的 extract）。
    这时用模板名作为 alias，保证文档里能看到它，而不是静默丢弃。
    """
    conns = raw.get("connections") or [{}]
    conn = conns[0]
    alias = (
        raw.get("alias")
        or raw.get("templateSource")
        or raw.get("templateName")
        or f"UNNAMED_EXTRACT_{index}"
    )
    flags = conn.get("requestFlags") or {}
    return ExtractSpec(
        alias=str(alias),
        connector=str(conn.get("connector", "unknown")),
        credentials=str(conn.get("credentials", "")),
        # 不同 connector 的"取数位置"字段名不同：
        #   cos/skapi/jdbc -> query    box -> file_name   部分 -> table
        # 只认 query 会把 box 类数据源读成空路径，文档里就变成"-"。
        query=str(
            conn.get("query")
            or conn.get("file_name")
            or conn.get("table")
            or conn.get("path")
            or ""
        ),
        main_df=bool(raw.get("main_df", False)),
        natural_key=tuple(str(k) for k in (conn.get("naturalKey") or ())),
        extract_type=str(raw.get("extractType", "default")),
        conn_alias=str(conn.get("alias", "")),
        key_name=str(conn.get("keyName", "")),
        flags=tuple((str(k), v) for k, v in flags.items()) if isinstance(flags, dict) else (),
        template_source=str(raw.get("templateSource", "")),
    )


def _transform(index: int, raw: dict) -> TransformSpec:
    ttype = str(raw["transformType"])
    # query 只有 SparkSql 表示 SQL；其他 transform 里 query 可能是路径
    sql = raw.get("query") if ttype == "SparkSql" else None
    detail = {
        k: v
        for k, v in raw.items()
        if k not in ("transformType", "objectNames", "aliasResult", "query")
    }
    return TransformSpec(
        index=index,
        transform_type=ttype,
        inputs=tuple(str(x) for x in (raw.get("objectNames") or ())),
        output=raw.get("aliasResult"),
        sql=sql,
        detail=detail,
    )


def _load(raw: dict | None) -> LoadSpec | None:
    if not raw:
        return None
    conns = raw.get("connections") or []
    if not conns:
        return None
    conn = conns[0]
    query = conn.get("query")
    if not query:
        return None
    objects = tuple(
        LoadObject(
            object_name=str(
                c.get("loadObject")
                or (raw.get("metadata") or {}).get("name")
                or str(c.get("query", "")).rsplit(".", 1)[-1]
            ),
            query=str(c.get("query", "")),
            mode=str(c.get("mode", "")),
            partitions=c.get("numPartitions"),
            partition_by=tuple(str(x) for x in (c.get("partitionBy") or ())),
            load_object=str(c.get("loadObject", "")),
        )
        for c in conns
        if c.get("query")
    )
    return LoadSpec(
        object_name=str((raw.get("metadata") or {}).get("name", "")),
        connector=str(conn.get("connector", "")),
        mode=str(conn.get("mode", "")),
        query=str(query),
        partitions=conn.get("numPartitions"),
        schema=_columns(raw.get("schema")),
        partition_by=tuple(str(p) for p in (conn.get("partitionBy") or ())),
        objects=objects,
        credentials=str(conn.get("credentials", "")),
        conn_alias=str(conn.get("alias", "")),
        table_space=str(conn.get("tableSpace", "")),
        flip_alias=bool(conn.get("flipAlias", False)),
    )


def parse_pipeline(path: str | Path) -> PipelineFacts:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PipelineParseError(f"{path.name} 不是合法 YAML") from exc
    except OSError as exc:
        raise PipelineParseError(f"读不到文件 {path}") from exc

    if not isinstance(data, dict):
        raise PipelineParseError(f"{path.name} 顶层不是 mapping")
    kind = data.get("kind")
    if kind not in KNOWN_KINDS:
        raise PipelineParseError(f"{path.name} 的 kind 是 {kind!r}，不是 pipeline")

    spec = data.get("spec") or {}
    if not isinstance(spec, dict):
        raise PipelineParseError(f"{path.name} 的 spec 不是 mapping")
    if not isinstance(spec.get("jobs"), list):
        raise PipelineParseError(f"{path.name} 的 spec.jobs 不是列表")

    jobs: list[JobSpec] = []
    for idx, raw_job in enumerate(spec["jobs"], 1):
        # 模型生成的 YAML 常见缺字段：整个 job 缺 jobType、transform 缺 transformType。
        # 这里统一转成 PipelineParseError，带上"第几个 job"——
        # 直接把 KeyError 抛给上层会让校验器崩掉，而校验器的职责是报告错误，不是崩溃。
        try:
            jobs.append(
                JobSpec(
                    job_type=str(raw_job["jobType"]),
                    extracts=tuple(
                        _extract(e, i) for i, e in enumerate(raw_job.get("extract") or ())
                    ),
                    transforms=tuple(
                        _transform(i, t)
                        for i, t in enumerate(raw_job.get("transform") or (), 1)
                    ),
                    load=_load(raw_job.get("load")),
                    size=str((raw_job.get("setups") or {}).get("size", "?")),
                )
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise PipelineParseError(
                f"第 {idx} 个 job 结构不完整（{type(exc).__name__}: {exc}）；"
                "每个 job 必须有 jobType，每个 transform 必须有 transformType"
            ) from exc

    return PipelineFacts(
        name=str((data.get("metadata") or {}).get("name", path.stem)),
        kind=str(kind),
        owner=str((spec.get("owner") or {}).get("name", "")),
        jobs=tuple(jobs),
        source_path=str(path),
    )
