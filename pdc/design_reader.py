"""设计文档（xlsx）-> DesignSpec：把"人写的文档"读回结构化需求。

这是 build 方向的入口。它和 design.py 是镜像关系：

    design.py         YAML  -> DesignDoc -> xlsx        （正向：文档生成）
    design_reader.py  xlsx  -> DesignSpec -> YAML 生成   （反向：配置生成）

为什么要有这一层，而不是把整张表塞进 prompt：
1. 表格里的列名/类型/分区是**契约**，必须逐字比对，不能靠模型"看着像"。
2. 有了结构化需求，校验器才能回答"你有没有漏掉一列"。
3. 同一份 spec 既能喂模型，也能喂离线基线——两者可比，才能证明模型值不值这个钱。

读取规则：**宽松读，严格报**。
表头可能被人改过（多空格、中英混排、大小写），所以按归一化后的名字匹配；
但匹配不上必需列时直接抛 SpecReadError，绝不猜。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

# 文档里写的是给人看的标签，YAML 里是机器值。这里做双向映射。
LABEL_TO_CONNECTOR = {
    "ibm cos": "cos",
    "cos": "cos",
    "skapi": "skapi",
    "jdbc": "jdbc",
    "jdbc (et)": "jdbc_et",
    "erdm api": "erdm_api",
}


class SpecReadError(ValueError):
    """设计文档读不出来。这条错误要原样给用户看，不要吞。"""


@dataclass(frozen=True)
class SpecColumn:
    name: str
    data_type: str
    precision: str = "-"
    nullable: bool = True
    partition: bool = False

    def signature(self) -> tuple:
        """比对用的签名：只含语义，不含展示格式。"""
        return (self.name, self.data_type.lower(), self.nullable, self.partition)


@dataclass(frozen=True)
class SpecExtract:
    job: str
    alias: str
    connector: str
    credentials: str
    query: str
    main_df: bool = False
    natural_key: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecTransform:
    job: str
    step: int
    transform_type: str
    inputs: tuple[str, ...] = ()
    output: str | None = None
    detail: str = ""
    # 语义参数（columns/mapColumns/columnsToSelect/...）。
    # detail 是给人看的文本，params 是给校验器比对的原始结构。
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SpecLoad:
    job: str
    object_name: str
    query: str
    mode: str = ""
    partitions: int | None = None
    partition_by: tuple[str, ...] = ()
    columns: tuple[SpecColumn, ...] = ()
    schema_declared: bool = True


@dataclass(frozen=True)
class DesignSpec:
    pipeline: str
    kind: str
    owner: str = ""
    sizing: str = ""
    jobs: tuple[str, ...] = ()
    extracts: tuple[SpecExtract, ...] = ()
    transforms: tuple[SpecTransform, ...] = ()
    loads: tuple[SpecLoad, ...] = ()
    source_path: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # ---------- 给模型看的视图 ----------

    def job_order(self) -> list[str]:
        return list(self.jobs)

    def columns_of(self, job: str) -> tuple[SpecColumn, ...]:
        for load in self.loads:
            if load.job == job:
                return load.columns
        return ()

    def to_prompt(self) -> str:
        """把需求压成紧凑文本。

        为什么不直接贴 xlsx 的原始行：模型会被 "#" 序号、"Note" 之类的
        展示列干扰，而这些列对生成没有任何意义。
        """
        lines = [
            f"pipeline: {self.pipeline}",
            f"kind: {self.kind}",
            f"owner: {self.owner}",
            f"job 顺序: {' -> '.join(self.jobs)}",
            "",
        ]
        for job in self.jobs:
            lines.append(f"## job {job}")

            exs = [e for e in self.extracts if e.job == job]
            if exs:
                lines.append("extract:")
                for e in exs:
                    nk = f" naturalKey={list(e.natural_key)}" if e.natural_key else ""
                    lines.append(
                        f"  - alias={e.alias} main_df={str(e.main_df).lower()} "
                        f"connector={e.connector} credentials={e.credentials} "
                        f"query={e.query}{nk}"
                    )

            txs = [t for t in self.transforms if t.job == job]
            if txs:
                lines.append("transform:")
                for t in txs:
                    detail = f" detail={t.detail}" if t.detail else ""
                    params = (
                        " params=" + json.dumps(t.params, ensure_ascii=False) if t.params else ""
                    )
                    lines.append(
                        f"  - step={t.step} type={t.transform_type} "
                        f"inputs={list(t.inputs)} output={t.output or '-'}{detail}{params}"
                    )

            for load in [x for x in self.loads if x.job == job]:
                cols = "" if load.schema_declared else "（文档未声明 schema，列由 connector 推断）"
                lines.append(
                    f"load: object={load.object_name} query={load.query} "
                    f"mode={load.mode or '-'} partitions={load.partitions if load.partitions is not None else '-'} "
                    f"partition_by={list(load.partition_by)}{cols}"
                )
                if load.schema_declared:
                    lines.append("  schema:")
                    for c in load.columns:
                        prec = "" if c.precision in ("-", "", None) else f" precision={c.precision}"
                        lines.append(
                            f"    - {c.name} {c.data_type}{prec} "
                            f"nullable={str(c.nullable).lower()} partition={str(c.partition).lower()}"
                        )
            lines.append("")
        if self.warnings:
            lines.append("读取告警（文档本身有缺失，生成时要显式告知用户）:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _truthy(value: object) -> bool:
    return _norm(value) in {"y", "yes", "true", "1", "是", "✓"}


def _find_sheet(wb, prefix: str):
    for name in wb.sheetnames:
        if name == prefix:
            return wb[name]
    for name in wb.sheetnames:
        if name.lower().startswith(prefix.lower()):
            return wb[name]
    return None


def _rows(ws) -> list[list[object]]:
    out = []
    for row in ws.iter_rows(values_only=True):
        if row is None:
            continue
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        out.append(list(row))
    return out


def _header_index(header: list[object]) -> dict[str, int]:
    idx: dict[str, int] = {}
    for i, cell in enumerate(header):
        key = _norm(cell)
        if key:
            idx[key] = i
    return idx


def _pick(idx: dict[str, int], *names: str) -> int | None:
    for n in names:
        if _norm(n) in idx:
            return idx[_norm(n)]
    return None


def _cell(row: list[object], i: int | None) -> object:
    if i is None or i >= len(row):
        return ""
    return row[i] if row[i] is not None else ""


def read_design(path: str | Path) -> DesignSpec:
    """读一份设计文档。只认同一种表结构：本仓库 design.py 产出的那份。

    这不是通用 xlsx 解析器，是**契约解析器**——表头变了就报错，
    因为静默读错比读不出来危险得多。
    """
    p = Path(path)
    if not p.exists():
        raise SpecReadError(f"设计文档不存在: {p}")
    try:
        wb = load_workbook(p, data_only=True, read_only=True)
    except Exception as exc:
        raise SpecReadError(f"打不开 xlsx: {exc}") from exc

    warnings: list[str] = []

    # ---------- Overview ----------
    ws = _find_sheet(wb, "Overview")
    if ws is None:
        raise SpecReadError("缺 Overview 表：这份文档不是本工具产出的，请先确认表结构")
    overview: dict[str, str] = {}
    for row in _rows(ws):
        if len(row) >= 2:
            key = _norm(row[0])
            if key:
                overview[key] = str(row[1] or "").strip()

    pipeline = overview.get("pipeline", "")
    kind = overview.get("kind", "")
    if not pipeline:
        raise SpecReadError("Overview 里读不到 Pipeline 名称")
    if not kind:
        warnings.append("Overview 缺 Kind，按 FactPipeline 处理")
        kind = "FactPipeline"

    sizing = overview.get("sizing", "")
    jobs: list[str] = []
    for part in sizing.split("/"):
        if ":" in part:
            jt = part.split(":", 1)[0].strip()
            if jt:
                jobs.append(jt)
    if not jobs:
        warnings.append("Overview 缺 Sizing，job 顺序改从 Source Inventory 推断")

    # ---------- Source Inventory ----------
    extracts: list[SpecExtract] = []
    ws = _find_sheet(wb, "Source Inventory")
    if ws is None:
        warnings.append("缺 Source Inventory 表，将无法生成 extract")
    else:
        rows = _rows(ws)
        if not rows:
            warnings.append("Source Inventory 是空的")
        else:
            idx = _header_index(rows[0])
            c_job = _pick(idx, "Job")
            c_alias = _pick(idx, "Alias")
            c_conn = _pick(idx, "Connector")
            c_cred = _pick(idx, "Credentials")
            c_query = _pick(idx, "Query / Path", "Query")
            c_main = _pick(idx, "main_df")
            c_nk = _pick(idx, "Natural key")
            if c_job is None or c_alias is None:
                raise SpecReadError("Source Inventory 缺 Job / Alias 列，表结构不对")
            for row in rows[1:]:
                alias = str(_cell(row, c_alias)).strip()
                if not alias or alias == "-":
                    continue
                conn_raw = _norm(_cell(row, c_conn))
                connector = LABEL_TO_CONNECTOR.get(conn_raw, conn_raw)
                nk_raw = str(_cell(row, c_nk)).strip()
                natural_key = tuple(
                    x.strip() for x in nk_raw.split(",") if x.strip() and x.strip() != "-"
                )
                extracts.append(
                    SpecExtract(
                        job=str(_cell(row, c_job)).strip(),
                        alias=alias,
                        connector=connector,
                        credentials=str(_cell(row, c_cred)).strip(),
                        query=str(_cell(row, c_query)).strip(),
                        main_df=_truthy(_cell(row, c_main)),
                        natural_key=natural_key,
                    )
                )
            if not jobs:
                for e in extracts:
                    if e.job and e.job not in jobs:
                        jobs.append(e.job)

    # ---------- Transform Chain ----------
    transforms: list[SpecTransform] = []
    ws = _find_sheet(wb, "Transform Chain")
    if ws is not None:
        rows = _rows(ws)
        if rows:
            idx = _header_index(rows[0])
            c_job = _pick(idx, "Job")
            c_step = _pick(idx, "Step")
            c_type = _pick(idx, "transformType")
            c_in = _pick(idx, "Inputs")
            c_out = _pick(idx, "Output")
            c_detail = _pick(idx, "Detail")
            c_params = _pick(idx, "Params")
            if c_type is None:
                warnings.append("Transform Chain 缺 transformType 列，transform 全部跳过")
            else:
                for row in rows[1:]:
                    ttype = str(_cell(row, c_type)).strip()
                    if not ttype or ttype == "-":
                        continue
                    raw_step = _cell(row, c_step)
                    try:
                        step = int(raw_step)
                    except (TypeError, ValueError):
                        step = len(transforms) + 1
                    raw_in = str(_cell(row, c_in)).strip()
                    inputs = tuple(x.strip() for x in raw_in.split(",") if x.strip() and x.strip() != "-")
                    out = str(_cell(row, c_out)).strip()
                    raw_params = str(_cell(row, c_params)).strip()
                    params: dict = {}
                    if raw_params:
                        try:
                            loaded = json.loads(raw_params)
                            if isinstance(loaded, dict):
                                params = loaded
                        except json.JSONDecodeError:
                            warnings.append(
                                f"transform {ttype} 的 Params 不是合法 JSON，已退回 Detail 文本"
                            )
                    transforms.append(
                        SpecTransform(
                            job=str(_cell(row, c_job)).strip(),
                            step=step,
                            transform_type=ttype,
                            inputs=inputs,
                            output=None if out in ("", "-") else out,
                            detail=str(_cell(row, c_detail)).strip(),
                            params=params,
                        )
                    )

    # ---------- Load Targets（谁写到哪） ----------
    targets: list[dict] = []
    ws = _find_sheet(wb, "Load Targets")
    if ws is not None:
        rows = _rows(ws)
        if rows:
            idx = _header_index(rows[0])
            c_job = _pick(idx, "Job")
            c_obj = _pick(idx, "Object")
            c_query = _pick(idx, "Target Path / Table", "Target Path")
            c_mode = _pick(idx, "Mode")
            c_parts = _pick(idx, "Partitions")
            c_pby = _pick(idx, "Partition By")
            c_schema = _pick(idx, "Schema")
            for row in rows[1:]:
                job = str(_cell(row, c_job)).strip()
                obj = str(_cell(row, c_obj)).strip()
                if not job or job == "-" or obj in ("", "-"):
                    continue
                raw_parts = _cell(row, c_parts)
                try:
                    parts = int(raw_parts) if str(raw_parts).strip() not in ("", "-", "None") else None
                except (TypeError, ValueError):
                    parts = None
                pby = tuple(
                    x.strip()
                    for x in str(_cell(row, c_pby)).split(",")
                    if x.strip() and x.strip() != "-"
                )
                targets.append(
                    {
                        "job": job,
                        "object_name": obj,
                        "query": str(_cell(row, c_query)).strip(),
                        "mode": str(_cell(row, c_mode)).strip(),
                        "partitions": parts,
                        "partition_by": pby,
                        "declared": _norm(_cell(row, c_schema)) == "declared",
                    }
                )

    # ---------- Landing × N（列级契约） ----------
    per_job_columns: dict[str, tuple[SpecColumn, ...]] = {}
    for name in wb.sheetnames:
        if not name.lower().startswith("landing"):
            continue
        ws = wb[name]
        rows = _rows(ws)
        if not rows:
            continue
        idx = _header_index(rows[0])
        c_name = _pick(idx, "Target Column")
        c_type = _pick(idx, "Data Type")
        c_prec = _pick(idx, "Precision")
        c_null = _pick(idx, "Nullable")
        c_part = _pick(idx, "Partition")
        if c_name is None or c_type is None:
            warnings.append(f"{name} 缺 Target Column / Data Type 列，跳过")
            continue
        job = name.split("-", 1)[1].strip() if "-" in name else name
        cols: list[SpecColumn] = []
        for row in rows[1:]:
            cname = str(_cell(row, c_name)).strip()
            if not cname or cname == "-" or cname.startswith("(该 job"):
                continue
            cols.append(
                SpecColumn(
                    name=cname,
                    data_type=str(_cell(row, c_type)).strip(),
                    precision=str(_cell(row, c_prec)).strip() or "-",
                    nullable=_truthy(_cell(row, c_null)),
                    partition=_truthy(_cell(row, c_part)) if c_part is not None else False,
                )
            )
        per_job_columns[job] = tuple(cols)

    # ---------- 合并成 loads ----------
    loads: list[SpecLoad] = []
    for t in targets:
        cols = per_job_columns.get(t["job"], ())
        loads.append(
            SpecLoad(
                job=t["job"],
                object_name=t["object_name"],
                query=t["query"],
                mode=t["mode"],
                partitions=t["partitions"],
                partition_by=t["partition_by"],
                columns=cols,
                schema_declared=bool(t["declared"] and cols),
            )
        )
    # 文档里没有 Load Targets（老旧格式）时，用 Landing 表兜底
    if not loads:
        for job, cols in per_job_columns.items():
            loads.append(
                SpecLoad(job=job, object_name="", query="", columns=cols, schema_declared=bool(cols))
            )
        if loads:
            warnings.append("缺 Load Targets 表，load 目标从 Landing 表推断")

    if not jobs:
        raise SpecReadError("文档里读不到任何 job")

    return DesignSpec(
        pipeline=pipeline,
        kind=kind,
        owner=overview.get("owner", ""),
        sizing=sizing,
        jobs=tuple(jobs),
        extracts=tuple(extracts),
        transforms=tuple(transforms),
        loads=tuple(loads),
        source_path=str(p),
        warnings=tuple(warnings),
    )
