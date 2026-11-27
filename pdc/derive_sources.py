"""从设计文档的自然语言里推导数据源（extract）。

为什么单独做这一层：

    设计文档的"变换说明"里写着 "join with COUNTRY_CODE_REF.csv"、
    "lookup against PERIOD_V4_DIMENSION_GREGORIAN.DATE"。
    这些既是变换，也是**数据源声明**。
    只把 extract 当成"主表一个"，产物就少了 8 个源，join 无从谈起。

判定规则（全部确定性，不调模型）：

    1. 主源：从"Landing source"那一格取，connector 固定 cos
    2. 其余：在变换说明里找 join / lookup / merge / against 后面的名字
    3. 名字里同时有表和列（A.B）时，取含 DIM/FACT 的那一段
    4. .csv 结尾的是文件，不是表；connector 推断为 box
    5. 关键词兜底：文本里出现 duns -> DIM_DUNS
    6. 历史/增量：同时出现 historical 和 delta 时，额外产出一个 dup job
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "join with X" / "lookup from X" / "merge with X" / "against X"
_LINK_PATTERNS = [
    re.compile(r"lookup\s+(?:from|against)\s+`?([A-Za-z0-9_.]+)`?", re.IGNORECASE),
    re.compile(r"join\s+with\s+`?([A-Za-z0-9_.]+)`?", re.IGNORECASE),
    re.compile(r"merge\s+with\s+`?([A-Za-z0-9_.]+)`?", re.IGNORECASE),
    re.compile(r"against\s+`?([A-Za-z0-9_.]+)`?", re.IGNORECASE),
]

# 文档里只写了业务词、没写表名的，靠这张表兜底。
# 这一层推不出来，只能人给。
_KEYWORD_SOURCES = {
    "duns": "DIM_DUNS",
}

# 文档里的写法 -> 真实表名。
_TABLE_ALIASES = {
    "DIM_GEOGRAPHY": "GEOGRAPHY_V3_DIMENSION",
    "DIM_MARKETING_INTENT_UT_MAPPING": "MARKETING_INTENT_UT_MAPPING_DIMENSION",
}

_TABLE_HINT = re.compile(r"(DIM|FACT)", re.IGNORECASE)


@dataclass(frozen=True)
class Source:
    """一个数据源。"""

    alias: str          # 产物里的 alias
    table: str          # 归一化后的表名/文件名
    raw: str            # 文档里的原话
    connector: str      # cos / box / skapi
    location: str       # query 或 file_name
    job: str = "eid"


def normalize_table(token: str) -> str:
    """把 'PERIOD_V4_DIMENSION_GREGORIAN.DATE.' 这类归一化成表名。"""
    token = token.strip("`. ")
    if not token:
        return ""
    if token.lower().endswith(".csv"):
        return token.upper()

    parts = [p for p in token.split(".") if p]
    if not parts:
        return ""
    for p in parts:
        if _TABLE_HINT.search(p):
            return _TABLE_ALIASES.get(p.upper(), p.upper())
    return _TABLE_ALIASES.get(parts[-1].upper(), parts[-1].upper())


def infer_connector(table: str) -> str:
    return "box" if table.upper().endswith(".CSV") else "cos"


def derive_sources(texts: list[str], landing_text: str = "") -> list[Source]:
    """从变换说明里推导数据源。

    texts：所有 Transformation 单元格的原文
    landing_text：文档里"Landing source"那一格的内容
    """
    found: dict[str, Source] = {}

    if landing_text.strip():
        first = landing_text.strip().splitlines()[0].strip()
        found["BOMBORA_DF"] = Source(
            alias="BOMBORA_DF",
            table="BOMBORA",
            raw=first[:80],
            connector="cos",
            location=first[:120],
            job="eid",
        )

    blob = "\n".join(texts)

    for text in texts:
        for pat in _LINK_PATTERNS:
            for m in pat.finditer(text or ""):
                table = normalize_table(m.group(1))
                if not table:
                    continue
                alias = table
                if alias in found:
                    continue
                found[alias] = Source(
                    alias=alias,
                    table=table,
                    raw=m.group(0)[:80],
                    connector=infer_connector(table),
                    location=table,
                    job="eid",
                )

    low = blob.lower()
    for keyword, table in _KEYWORD_SOURCES.items():
        if keyword in low and table not in found:
            found[table] = Source(
                alias=table,
                table=table,
                raw=keyword,
                connector="cos",
                location=table,
                job="eid",
            )

    if "historical" in low and "delta" in low:
        found["BOMBORA_HIST_DF"] = Source(
            alias="BOMBORA_HIST_DF", table="BOMBORA", raw="historical",
            connector="cos", location="/DATE_WEEK=*/*", job="eid-sftp-dup",
        )
        found["BOMBORA_DELTA_DF"] = Source(
            alias="BOMBORA_DELTA_DF", table="BOMBORA", raw="delta",
            connector="cos", location="/*", job="eid-sftp-dup",
        )

    return list(found.values())
