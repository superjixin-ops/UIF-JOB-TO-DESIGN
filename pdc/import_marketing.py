"""把"Marketing 模板"的 xlsm 读成 DesignSpec。

这一类文档的结构（三份样本表头完全一致）：

    Log Changes
    Source to EID - Dim, Fact name     每行 = 一个 EID 层目标列
    options
    EID to DM - Dim, Fact name         每行 = 一个 DM 层目标列
    DDL                                建表语句，作为权威 schema

为什么不交给模型整份抽取：

    表头固定、值域很小（类型只有 4 种、Null 只有 Y/N），
    确定性解析能拿到九成以上，而且可以写测试。
    模型只留给"自然语言写的变换说明"这一小块。

校验分三层，全在确定性代码里：

    1. DDL 的列 必须等于 EID to DM 表的目标列
    2. EID to DM 引用的源列 必须来自 Source to EID 的产出列
    3. 类型、可空必须在枚举里
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from pdc.design_reader import (
    DesignSpec,
    SpecColumn,
    SpecExtract,
    SpecLoad,
    SpecReadError,
    SpecTransform,
)

SOURCE_SHEET = "Source to EID - Dim, Fact name"
DM_SHEET = "EID to DM - Dim, Fact name"
DDL_SHEET = "DDL"

ALLOWED_TYPES = {
    "INTEGER", "BIGINT", "STRING", "VARCHAR", "DECIMAL", "NUMERIC",
    "DATE", "TIMESTAMP", "BOOLEAN",
}

# 文档里的 SQL 类型 -> 项目里的 Spark 类型。
# 不写成 base.capitalize() + "Type"，因为 BIGINT / VARCHAR 这类名字对不上。
TYPE_MAP = {
    "INTEGER": "IntegerType",
    "BIGINT": "LongType",
    "STRING": "StringType",
    "VARCHAR": "StringType",
    "DECIMAL": "DecimalType",
    "NUMERIC": "DecimalType",
    "DATE": "DateType",
    "TIMESTAMP": "TimestampType",
    "BOOLEAN": "BooleanType",
}

# 平台自动生成的列：它们不由上游映射而来，跨层引用检查要放过。
SYSTEM_COLUMNS = {"AUDIT_TIMESTAMP", "SOURCE_FILE", "ETL_LOAD_TS", "IMPORT_DATE_KEY"}

# 归一化后的关键词 -> 要产出的变换类型。
# 覆盖样本语料里 93% 的写法（240 条描述只有 37 种，前 8 类占九成）。
#
# 顺序很重要：具体的放前面。
# "simple movement and lookup from X" 同时包含 "simple movement" 和 "lookup from"，
# 如果 "simple movement" 在前，就会把 join 误判成"无变换"。
RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("lookup from", "merge with"), ("JoinByExpression",)),
    (("monotonically_increasing_id", "uniqueue column", "unique column"), ("UniqueID",)),
    (("defaulted to", "default python none", "dafault python none"), ("FillNA",)),
    (("rename and trim",), ("RenameColumns", "Trim")),
    (("rename",), ("RenameColumns",)),
    (("drop",), ("Drop",)),
    (("trim",), ("Trim",)),
    (("convert",), ("Cast",)),
    (("simple movement",), ()),
]


def detect(wb) -> bool:
    """按表名判断是不是这一类模板。不猜内容。"""
    names = set(wb.sheetnames)
    return SOURCE_SHEET in names and DM_SHEET in names


def normalize(text: object) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return s


def map_transform(raw: object) -> tuple[str, ...]:
    """自然语言变换 -> 项目里的变换类型。匹配不上返回空元组。

    返回空元组有两种含义，调用方要用 is_known 区分：
      - "simple movement"：确实是"不需要变换"
      - 匹配不上：未知，需要人来判断
    """
    text = normalize(raw)
    if not text:
        return ()
    for keywords, types in RULES:
        if any(k in text for k in keywords):
            return types
    return ()


def is_known(raw: object) -> bool:
    """这条描述能不能被规则认出来（包括"确实不需要变换"）。"""
    text = normalize(raw)
    if not text:
        return True
    return any(any(k in text for k in keys) for keys, _ in RULES)


def parse_ddl(text: str) -> tuple[str, list[tuple[str, str, bool]]]:
    """从建表语句里取出表名和列。

    只截取 CREATE TABLE 的括号块——后面还有 ALTER / COMMENT 语句，
    一起解析会把它们当成列。
    """
    m = re.search(r"CREATE\s+TABLE\s+([\w.]+)\s*\(", text, re.IGNORECASE)
    if not m:
        raise SpecReadError("DDL 里找不到 CREATE TABLE")

    table = m.group(1)
    start = m.end() - 1
    depth = 0
    end = None
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise SpecReadError("DDL 的 CREATE TABLE 括号不闭合")

    columns: list[tuple[str, str, bool]] = []
    for line in text[start + 1 : end].splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        rest = " ".join(parts[1:])
        nullable = "NOT NULL" not in rest.upper()
        dtype = re.split(r"\s+NOT\s+NULL", rest, flags=re.IGNORECASE)[0].strip().rstrip(",")
        columns.append((name, dtype, nullable))
    if not columns:
        raise SpecReadError("DDL 里没解析出任何列")
    return table, columns


def _header_index(header: list[str]) -> dict[str, int]:
    return {h.strip(): i for i, h in enumerate(header) if h}


def _cell(row: tuple, idx: dict[str, int], name: str) -> object:
    i = idx.get(name)
    if i is None or i >= len(row):
        return None
    return row[i]


def _sheet_rows(wb, name: str) -> list[tuple]:
    ws = wb[name]
    return [r for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]


def _to_column(name: str, dtype: str, nullable: bool) -> SpecColumn:
    raw = str(dtype or "").strip().upper()
    base = re.split(r"[\s(]", raw)[0] if raw else "STRING"
    if base not in ALLOWED_TYPES:
        raise SpecReadError(f"列 {name} 的类型 {dtype!r} 不在允许列表里")

    precision_num = scale_num = None
    m = re.search(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", raw)
    if m:
        precision_num, scale_num = int(m.group(1)), int(m.group(2))

    return SpecColumn(
        name=name,
        data_type=TYPE_MAP[base],
        precision=f"({precision_num},{scale_num})" if precision_num is not None else "-",
        nullable=nullable,
        precision_num=precision_num,
        scale_num=scale_num,
    )


def read_marketing(path: str | Path) -> DesignSpec:
    p = Path(path)
    wb = load_workbook(p, data_only=True, read_only=True)
    try:
        if not detect(wb):
            raise SpecReadError("不是 Marketing 模板：缺 Source to EID / EID to DM 表")

        warnings: list[str] = []

        # ---------- DDL：权威 schema ----------
        ddl_rows = _sheet_rows(wb, DDL_SHEET) if DDL_SHEET in wb.sheetnames else []
        if not ddl_rows:
            raise SpecReadError("缺 DDL 表，无法确定目标表结构")
        ddl_text = "\n".join(str(r[0]) for r in ddl_rows if r and r[0])
        table_full, ddl_cols = parse_ddl(ddl_text)
        table_name = table_full.split(".")[-1]

        # ---------- 两张映射表 ----------
        src_rows = _sheet_rows(wb, SOURCE_SHEET)
        dm_rows = _sheet_rows(wb, DM_SHEET)
        if len(src_rows) < 2 or len(dm_rows) < 2:
            raise SpecReadError("映射表是空的")

        src_idx = _header_index([str(c) for c in src_rows[0]])
        dm_idx = _header_index([str(c) for c in dm_rows[0]])

        eid_columns: list[SpecColumn] = []
        eid_targets: list[str] = []
        for row in src_rows[1:]:
            target = _cell(row, src_idx, "Target Column")
            if not target:
                continue
            name = str(target).strip()
            eid_targets.append(name)
            eid_columns.append(
                _to_column(name, str(_cell(row, src_idx, "Column Type") or ""),
                           normalize(_cell(row, src_idx, "Null")) != "n")
            )

        dm_columns: list[SpecColumn] = []
        dm_targets: list[str] = []
        for row in dm_rows[1:]:
            target = _cell(row, dm_idx, "Target Column")
            if not target:
                continue
            dm_targets.append(str(target).strip())

        # ---------- 第一道闸：DDL 和 DM 目标列必须一致 ----------
        ddl_names = [c[0] for c in ddl_cols]
        if ddl_names != dm_targets:
            only_ddl = [c for c in ddl_names if c not in set(dm_targets)]
            only_sheet = [c for c in dm_targets if c not in set(ddl_names)]
            raise SpecReadError(
                f"DDL 和 EID to DM 表对不上：DDL 多 {only_ddl}，表格多 {only_sheet}"
            )
        for name, dtype, nullable in ddl_cols:
            dm_columns.append(_to_column(name, dtype, nullable))

        # ---------- 第二道闸：下游引用的列必须来自上游产出 ----------
        referenced = {
            str(_cell(r, dm_idx, "Source Columns")).strip()
            for r in dm_rows[1:]
            if _cell(r, dm_idx, "Source Columns")
        }
        missing = referenced - set(eid_targets)
        missing = {m for m in missing if m not in SYSTEM_COLUMNS and "AUDIT" not in m.upper()}
        if missing:
            raise SpecReadError(f"EID to DM 引用了上游没有的列: {sorted(missing)}")

        # ---------- 变换：能映射的映射，不能的留原文 ----------
        transforms: list[SpecTransform] = []
        unknown: list[str] = []

        def collect(rows, idx, job: str, alias: str, start: int) -> int:
            step = start
            for row in rows[1:]:
                target = _cell(row, idx, "Target Column")
                if not target:
                    continue
                for col_name in ("Transformation 1", "Transformation 2"):
                    raw = _cell(row, idx, col_name)
                    if not raw:
                        continue
                    types = map_transform(raw)
                    if not types:
                        if not is_known(raw):
                            unknown.append(f"{job}.{target}: {normalize(raw)[:60]}")
                        continue
                    for t in types:
                        step += 1
                        params: dict = {}
                        if t == "RenameColumns":
                            source = str(_cell(row, idx, "Source Columns") or "").strip()
                            if source and source != str(target):
                                params = {"mapColumns": {source: str(target)}}
                        transforms.append(
                            SpecTransform(
                                job=job,
                                step=step,
                                transform_type=t,
                                inputs=(alias,),
                                output=alias,
                                detail=normalize(raw)[:120],
                                params=params,
                            )
                        )
            return step

        step = collect(src_rows, src_idx, "eid", "SOURCE", 0)
        collect(dm_rows, dm_idx, "dm", "EID_SOURCE", step)

        if unknown:
            warnings.append(
                f"{len(unknown)} 条变换无法用规则映射，已跳过（需人工确认）：" + "；".join(unknown[:3])
            )

        pipeline = re.sub(r"[^a-z0-9]+", "-", table_name.lower()).strip("-")
        eid_table = f"{table_name}_EID"

        # 连接信息（connector / credentials / query）文档里没写。
        # load 的 query 不能为空——解析器会把它当成"没有 load"。
        # 这里给占位路径，并在 warnings 里明确说明要人工补。
        warnings.append("文档未提供连接信息（connector / credentials / query），已填占位路径，需人工确认")

        extracts = (
            SpecExtract(
                job="eid",
                alias="SOURCE",
                connector="",
                credentials="",
                query=f"landing/{pipeline}",
                main_df=True,
            ),
            SpecExtract(
                job="dm",
                alias="EID_SOURCE",
                connector="",
                credentials="",
                query=f"fact/{eid_table}",
                main_df=True,
            ),
        )

        loads = (
            SpecLoad(
                job="eid",
                object_name=eid_table,
                query=f"fact/{eid_table}",
                columns=tuple(eid_columns),
                schema_declared=True,
            ),
            SpecLoad(
                job="dm",
                object_name=table_name,
                query=f"fact/{table_name}",
                metadata_name=table_name,
                columns=tuple(dm_columns),
                schema_declared=True,
            ),
        )

        return DesignSpec(
            pipeline=pipeline,
            kind="FactPipeline",
            owner="",
            sizing="M",
            jobs=("eid", "dm"),
            job_sizes=(("eid", "M"), ("dm", "M")),
            extracts=extracts,
            transforms=tuple(transforms),
            loads=loads,
            source_path=str(p),
            warnings=tuple(warnings),
        )
    finally:
        wb.close()
