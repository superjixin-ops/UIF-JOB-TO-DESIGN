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



def merge_renames(transforms: list[SpecTransform]) -> list[SpecTransform]:
    """把同一个 job 里所有的 RenameColumns 合成一步。

    文档里一列一行，天然会生成 20 个独立改名。
    20 步会把提示词挤满，模型就没注意力看后面的 join / SparkSql 参数了。
    合并之后语义不变（改名互不影响），提示词短一大截。
    """
    from dataclasses import replace as _replace

    merged: dict[str, dict] = {}
    first_at: dict[str, int] = {}
    out: list[SpecTransform] = []
    for t in transforms:
        if t.transform_type != "RenameColumns":
            out.append(t)
            continue
        mapping = t.params.get("mapColumns") or {}
        if not mapping:
            continue
        if t.job not in first_at:
            first_at[t.job] = len(out)
            out.append(t)
            merged[t.job] = {}
        merged[t.job].update(mapping)

    for job, at in first_at.items():
        out[at] = _replace(out[at], params={"mapColumns": merged[job]})
    return out


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


def read_marketing(path: str | Path, llm=None) -> DesignSpec:
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
        unknown: list[dict] = []

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
                            unknown.append({"job": job, "target": str(target), "raw": normalize(raw)[:400]})
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

        if unknown and llm is not None:
            for job in sorted({u["job"] for u in unknown}):
                batch = [
                    {"target": u["target"], "raw": u["raw"]}
                    for u in unknown
                    if u["job"] == job
                ]
                translated = translate_batch(llm, batch)
                done = set()
                for target, item in translated.items():
                    if item["confidence"] < 0.5:
                        continue
                    for st in item["steps"]:
                        step += 1
                        transforms.append(
                            SpecTransform(
                                job=job,
                                step=step,
                                transform_type=st["transformType"],
                                inputs=("SOURCE",) if job == "eid" else ("EID_SOURCE",),
                                output="SOURCE" if job == "eid" else "EID_SOURCE",
                                detail=f"model(conf={item['confidence']:.2f})",
                                params=st["params"],
                            )
                        )
                    done.add(target)
                unknown = [u for u in unknown if not (u["job"] == job and u["target"] in done)]

        if unknown:
            names = [f"{u['job']}.{u['target']}" for u in unknown]
            warnings.append(
                f"{len(unknown)} 条变换无法翻译，已跳过（需人工确认）：" + "；".join(names[:5])
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
            weak=True,
        )
    finally:
        wb.close()


# ---------------------------------------------------------------- 单表模板
# 另一类文档：只有一张表，没有 DDL。
# 表头里有 "Target Column" / "Column Type" / "Null"，按名字定位即可。
# 少了 DDL 这道闸，只剩类型枚举和重名检查——所以警告里要说清楚。


def detect_single(wb) -> bool:
    """第一张表的表头里有 Target Column，且不是前一种模板。"""
    if detect(wb):
        return False
    ws = wb[wb.sheetnames[0]]
    first = next(ws.iter_rows(max_row=1, values_only=True), ())
    return any(str(c).strip() == "Target Column" for c in first if c)


def _pipeline_from_text(text: str) -> str:
    """从 'fact/FACT_MARKET_INTENT_BOMBORA' 这类路径里取流水线名。

    取不到返回空串，让调用方决定用什么兜底——不在这里编一个名字。
    """
    m = re.search(r"fact/([A-Za-z0-9_]+)", text or "")
    if not m:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-")


def read_single(path: str | Path, llm=None) -> DesignSpec:
    p = Path(path)
    wb = load_workbook(p, data_only=True, read_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = [r for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
        if not rows:
            raise SpecReadError("表格是空的")

        header = [str(c).strip() if c else "" for c in rows[0]]
        idx = {h: i for i, h in enumerate(header) if h}
        if "Target Column" not in idx:
            raise SpecReadError("表头里找不到 Target Column")

        warnings: list[str] = ["这份文档没有 DDL 表，列契约只做了类型和重名校验"]

        columns: list[SpecColumn] = []
        seen: set[str] = set()
        for row in rows[1:]:
            name = _cell(row, idx, "Target Column")
            if not name:
                continue
            name = str(name).strip()
            if name in seen:
                raise SpecReadError(f"目标列重名: {name}")
            seen.add(name)
            columns.append(
                _to_column(
                    name,
                    str(_cell(row, idx, "Column Type") or ""),
                    normalize(_cell(row, idx, "Null")) != "n",
                )
            )
        if not columns:
            raise SpecReadError("没有解析出任何目标列")

        # 变换
        transforms: list[SpecTransform] = []
        step = 0
        unknown: list[dict] = []
        for row in rows[1:]:
            name = _cell(row, idx, "Target Column")
            if not name:
                continue
            for col_name in ("Transformation 1", "Transformation 2"):
                raw = _cell(row, idx, col_name)
                if not raw:
                    continue
                types = map_transform(raw)
                if not types:
                    if not is_known(raw):
                        unknown.append({"target": str(name), "raw": normalize(raw)[:400]})
                    continue
                for t in types:
                    step += 1
                    params: dict = {}
                    if t == "RenameColumns":
                        # 改名的来源列：优先用"中间结果"，没有就退到源列。
                        # 不填 mapColumns 的话，生成的 RenameColumns 是空操作。
                        source = ""
                        for candidate in ("Intermediate Result 1", "Json Source Columns", "Historical Intent"):
                            v = _cell(row, idx, candidate)
                            if v:
                                source = str(v).strip()
                                break
                        if source and source != str(name):
                            params = {"mapColumns": {source: str(name)}}
                    transforms.append(
                        SpecTransform(
                            job="eid",
                            step=step,
                            transform_type=t,
                            inputs=("SOURCE",),
                            output="SOURCE",
                            detail=normalize(raw)[:120],
                            params=params,
                        )
                    )
        if unknown and llm is not None:
            raw_by_target = {u["target"]: u["raw"] for u in unknown}
            translated = translate_batch(llm, unknown)
            done = set()
            for target, item in translated.items():
                if item["confidence"] < 0.5:
                    continue
                for st in item["steps"]:
                    step += 1
                    transforms.append(
                        SpecTransform(
                            job="eid",
                            step=step,
                            transform_type=st["transformType"],
                            inputs=("SOURCE",),
                            output="SOURCE",
                            detail=f"原文: {raw_by_target.get(target, '')[:100]}",
                            params=st["params"],
                        )
                    )
                done.add(target)
            unknown = [u for u in unknown if u["target"] not in done]

        if unknown:
            names = [u["target"] for u in unknown]
            warnings.append(
                f"{len(unknown)} 条变换无法翻译，已跳过（需人工确认）："
                + "；".join(names[:5])
            )

        # 流水线名：输出路径可能写在表头格里，也可能写在第一行数据里。
        # 两处都扫一遍，取不到再用文件名兜底。
        out_text = " ".join(str(c) for r in rows[:2] for c in r if c)
        pipeline = _pipeline_from_text(out_text) or re.sub(
            r"[^a-z0-9]+", "-", p.stem.lower()
        ).strip("-")
        table_name = pipeline.replace("-", "_").upper()

        warnings.append("文档未提供连接信息，已填占位路径，需人工确认")

        return DesignSpec(
            pipeline=pipeline,
            kind="FactPipeline",
            owner="",
            sizing="M",
            jobs=("eid",),
            job_sizes=(("eid", "M"),),
            extracts=(
                SpecExtract(
                    job="eid", alias="SOURCE", connector="", credentials="",
                    query=f"landing/{pipeline}", main_df=True,
                ),
            ),
            transforms=tuple(merge_renames(transforms)),
            loads=(
                SpecLoad(
                    job="eid",
                    object_name=table_name,
                    query=f"fact/{table_name}",
                    metadata_name=table_name,
                    columns=tuple(columns),
                    schema_declared=True,
                ),
            ),
            source_path=str(p),
            warnings=tuple(warnings),
            weak=True,
        )
    finally:
        wb.close()


# ---------------------------------------------------------------- 模型翻译
# 规则表只认短句式（"simple movement"、"rename"）。
# 长段落是业务规则，必须交给模型翻译成**带参数**的步骤，
# 而不是跳过——跳过的恰好是 join / hash / 去重这些真正重要的逻辑。

SUPPORTED_TYPES = {
    "RenameColumns", "Trim", "Drop", "Cast", "Select", "FilterType2",
    "JoinByExpression", "AddColumns", "FillNA", "UniqueID", "SparkSql",
    "CurrentDate", "AddRow",
}

TRANSLATE_SYSTEM = """你是数据管道变换翻译器。把业务规则翻译成结构化步骤。

可用类型和参数：
- RenameColumns: {"mapColumns": {"旧列": "新列"}}
- Trim: {"columns": ["列"]}
- Drop: {"columns": ["列"]}
- Cast: {"columns": ["列"], "dataType": "IntegerType"}
- Select: {"columns": ["列"]}
- FilterType2: {"condition": "SQL 条件"}
- JoinByExpression: {"joinExpression": "A.x = B.y", "joinType": "left", "columnsToSelect": ["A.*"]}
- AddColumns: {"columns": [{"name": "X", "value": "SQL 表达式"}]}
- FillNA: {"columns": ["列"], "value": "填充值"}
- UniqueID: {"columns": ["列"]}
- SparkSql: {"query": "完整 SELECT 语句"}
- CurrentDate: {"columns": ["列"]}
- AddRow: {"values": {"列": "值"}}

要求：
1. 只输出 JSON，不要解释。
2. 格式：{"目标列名": {"confidence": 0到1, "steps": [{"transformType": "...", "params": {...}}]}}
3. 不确定就给低 confidence，不要编造列名。
4. 如果逻辑复杂到无法用上面的类型表达，用 SparkSql，query 写完整 SELECT。
"""


def _extract_json(text: str) -> dict:
    """从模型回复里抠出第一个 JSON 对象。"""
    import json as _json

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = _json.loads(text[start : end + 1])
    except _json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def translate_batch(llm, items: list[dict]) -> dict[str, dict]:
    """一次调用翻译多条规则。返回 {目标列: {"confidence":..., "steps":[...]}}。"""
    import json as _json

    if not items:
        return {}
    user = _json.dumps(items, ensure_ascii=False, indent=2)
    try:
        reply = llm.chat(
            [
                {"role": "system", "content": TRANSLATE_SYSTEM},
                {"role": "user", "content": user},
            ],
            [],
        )
    except Exception:  # noqa: BLE001 - 翻译失败不能拖垮整份文档
        return {}
    data = _extract_json(reply.content or "")
    out: dict[str, dict] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        steps = value.get("steps")
        if not isinstance(steps, list):
            continue
        clean = []
        for st in steps:
            if not isinstance(st, dict):
                continue
            t = str(st.get("transformType", "")).strip()
            if t not in SUPPORTED_TYPES:
                continue
            params = st.get("params")
            clean.append({"transformType": t, "params": params if isinstance(params, dict) else {}})
        if clean:
            out[key] = {
                "confidence": float(value.get("confidence") or 0),
                "steps": clean,
            }
    return out
