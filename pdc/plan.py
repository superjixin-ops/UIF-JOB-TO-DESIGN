"""设计文档 -> 实现计划（确定性）。

为什么要有"计划"这一层：

    设计文档写的是"要什么"，YAML 写的是"怎么做"。中间那一步
    —— 先 prep 维表、再改名、再 join、再算键、最后裁剪 —— 是**编译**，
    不是翻译。交给模型做，它每一轮都会漏掉不同的十步。

    这一层把"怎么做"固化成规则，按模板家族分发：

        marketing_intent   营销意图事实表（本文件）
        其他               返回 None，退回模型

    规则只依赖文档里**写明的事实**（源列名、join 的表、去重键、
    空值规则），不依赖某一份具体文件。换一份同家族的文档，
    列名变了，计划跟着变。

    模型的职责缩小到"自然语言段落 -> 表达式"。它不再需要记住
    71 个变换的顺序和参数——那不是语言能力，是编译。
"""

from __future__ import annotations

import re
from typing import Any

from pdc.design_reader import DesignSpec, SpecColumn
from pdc.profiles import API_VERSION, MARKETING_INTENT

# 物理表 -> pipeline 内的写法。约定放在 profiles.py，
# 解析器和计划器共用一份，避免两边各认一套别名。
_SOURCE_LAYOUT = MARKETING_INTENT

# 维表里的物理列名。文档写了业务名，没写列名，只能在这里给一次。
_DIM_COLUMNS = {
    "geography_key": "SURROGATE_KEY",
    "geography_code": "CODE",
    "company_key": "SK_LOCATION_COMPANY_BUSINESS_VIEW",
    "company_domain": "COMPANY_DOMAIN",
    "company_landed_country": "COMPANY_LANDED_COUNTRY",
    "company_duns": "SK_COMPANY_DUNS",
    "duns_key": "SK_DUNS",
    "duns_postal": "POSTAL_CODE",
    "duns_phone": "PHONE",
    "ut_category": "INTENT_CATEGORY_NAME",
    "ut_topic": "INTENT_PRODUCT_NAME",
    "ut_code": "UT_CODE",
    "ut_mapping_key": "SURROGATE_KEY",
    "ut_status": "MAPPING_STATUS",
    "period_date": "DATE",
    "period_key": "SURROGATE_KEY",
}

# 主源里这些列是自由文本，源数据可能写的是字符串 "null"/"NaN"。
# FillNA 只处理 NULL，处理不了字符串，所以先按列兜一层。
_TEXT_PLACEHOLDER_PREFIXES = ("HQ ",)


# ---------------------------------------------------------------- SQL 片段
def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _col(name: str) -> str:
    """含空格/特殊字符的列名要加反引号，否则 Spark 会把 HQ Country 拆成两个词。"""
    return f"`{name}`" if re.search(r"[^A-Za-z0-9_]", name) else name


def _expr(sql: str) -> str:
    """UIF 的 AddColumns 用 expr(...) 包 SQL。"""
    return f'expr("{sql}")'


def _null_literal(name: str, literal: str) -> str:
    c = _col(name)
    return _expr(
        f"CASE WHEN {c} IS NULL OR trim({c}) = '' THEN {_lit(literal)} ELSE {c} END"
    )


def _invalid_literal(name: str, literal: str, invalid: tuple[str, ...]) -> str:
    c = _col(name)
    items = ", ".join(_lit(x) for x in invalid)
    return _expr(
        f"CASE WHEN {c} IS NULL OR upper(trim({c})) IN ({items}) "
        f"THEN {_lit(literal)} ELSE {c} END"
    )


def _norm(name: str) -> str:
    """去掉非字母数字并转大写。邮编/电话/国家都按这个口径比较。"""
    return (
        "regexp_replace(upper(trim(" + name + ")), '[^A-Z0-9]', '')"
    )


# ---------------------------------------------------------------- 计划构建器
class _Plan:
    """按顺序收集变换。

    顺序就是语义：join 必须排在它依赖的列出现之后。
    所以这里不做排序，只做追加——排序会让"为什么这一步在这里"消失。
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        ttype: str,
        obj: str | list[str],
        alias: str | None = None,
        **params: Any,
    ) -> None:
        item: dict[str, Any] = {
            "transformType": ttype,
            "objectNames": [obj] if isinstance(obj, str) else list(obj),
        }
        for key, value in params.items():
            if value is not None:
                item[key] = value
        item["aliasResult"] = alias or (obj if isinstance(obj, str) else obj[0])
        self.items.append(item)


# ---------------------------------------------------------------- 小工具
def _fact(spec: DesignSpec, key: str, default: Any = None) -> Any:
    return (spec.facts or {}).get(key, default)


def _alias_of(spec: DesignSpec, source: str) -> str:
    layout = _SOURCE_LAYOUT.get(source, {})
    return str(layout.get("alias") or source)


def _has(spec: DesignSpec, source: str) -> bool:
    return any(e.alias == source for e in spec.extracts)


def _schema(columns: tuple[SpecColumn, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in columns:
        item: dict[str, Any] = {"columnName": c.name, "dataType": c.data_type}
        if c.precision_num is not None:
            args: dict[str, Any] = {"precision": c.precision_num}
            if c.scale_num is not None:
                args["scale"] = c.scale_num
            item["dataTypeArgs"] = args
        item["nullAble"] = c.nullable
        out.append(item)
    return out


def _job(
    job_type: str,
    size: str,
    extracts: list[dict[str, Any]],
    transforms: list[dict[str, Any]],
    load: dict[str, Any],
    spark_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    setups: dict[str, Any] = {"size": size}
    if spark_config:
        setups["spark_config"] = spark_config
    return {
        "jobType": job_type,
        "setups": setups,
        "extract": extracts,
        "transform": transforms,
        "load": load,
    }


def _extract(
    alias: str,
    connector: str,
    credentials: str,
    main_df: bool,
    connection: dict[str, Any],
) -> dict[str, Any]:
    return {
        "extractType": "default",
        "alias": alias,
        "main_df": main_df,
        "connections": [{"credentials": credentials, "connector": connector, **connection}],
    }


# ---------------------------------------------------------------- 阶段一：维表准备
def _prep_dimensions(p: _Plan, spec: DesignSpec) -> None:
    """维表不能直接 join。

    每一张都要先过滤（只留有效版本）、去重、改名——
    不过滤会把已删除的历史版本 join 进来，一条事实变成十条。
    """
    geo = _alias_of(spec, "GEOGRAPHY_V3_DIMENSION")
    cc = _alias_of(spec, "COUNTRY_CODE_REF.CSV")
    lc = _alias_of(spec, "LOCATION_COMPANY_V2_DIMENSION")
    duns = _alias_of(spec, "DIM_DUNS")
    ut = _alias_of(spec, "MARKETING_INTENT_UT_MAPPING_DIMENSION")
    d = _DIM_COLUMNS

    if _has(spec, "COUNTRY_CODE_REF.CSV"):
        p.add("Trim", cc, columns=["CTRY_NAME", "CTRY"])
        p.add("AddColumns", cc, columns=["CTRY_NAME"], value=_expr("upper(CTRY_NAME)"))
        p.add("SelectDistinct", cc, columns=["CTRY_NAME", "CTRY"])

    if _has(spec, "GEOGRAPHY_V3_DIMENSION"):
        p.add("FilterData", geo, whereClause="(LEVEL = 5)")
        p.add(
            "SelectDistinct",
            geo,
            columns=[d["geography_key"], d["geography_code"]],
        )
        p.add(
            "FilterData",
            geo,
            whereClause=(
                f"{d['geography_key']} IS NOT NULL AND {d['geography_code']} IS NOT NULL"
            ),
        )
        p.add(
            "RenameColumns",
            geo,
            mapColumns={
                d["geography_key"]: "COMPANY_COUNTRY_KEY",
                d["geography_code"]: "COUNTRY_CODE",
            },
        )

    if _has(spec, "LOCATION_COMPANY_V2_DIMENSION"):
        p.add(
            "FilterType2",
            lc,
            groupByKey=d["company_key"],
            includeDeleted=False,
        )
        # 候选数必须在 DUNS / 国家补充之前算完。
        # 先 join 再 count，一条公司会被邮编行放大成多条，候选数就假了。
        p.add(
            "SparkSql",
            lc,
            alias="LOCATION_COMPANY_DOMAIN_COUNTS",
            query=(
                f"SELECT lower(trim({d['company_domain']})) AS LOCATION_MATCH_DOMAIN, "
                f"COUNT(DISTINCT {d['company_key']}) AS LOCATION_CANDIDATE_COUNT "
                f"FROM {lc} "
                f"WHERE {d['company_key']} > 0 "
                f"GROUP BY lower(trim({d['company_domain']}))"
            ),
        )
        if _has(spec, "COUNTRY_CODE_REF.CSV"):
            p.add(
                "JoinByExpression",
                [lc, cc],
                joinExpression=(
                    f"upper(trim({lc}.{d['company_landed_country']})) = "
                    f"upper(trim({cc}.CTRY)) AND "
                    f"upper(trim({lc}.{d['company_landed_country']})) "
                    "NOT IN ('', 'UNASSIGNED', 'NULL', 'NAN', 'NONE')"
                ),
                joinType="left",
                columnsToSelect=[
                    f"{lc}.*",
                    f"{cc}.CTRY_NAME AS COMPANY_LANDED_COUNTRY_NAME",
                ],
            )

    if _has(spec, "DIM_DUNS"):
        p.add("FilterType2", duns, groupByKey=d["duns_key"], includeDeleted=False)

    if _has(spec, "MARKETING_INTENT_UT_MAPPING_DIMENSION"):
        p.add("FilterType2", ut, groupByKey=d["ut_mapping_key"], includeDeleted=False)


# ---------------------------------------------------------------- 阶段二：主源
def _prep_main(p: _Plan, spec: DesignSpec, main: str, dup_keys: list[str]) -> None:
    source_map: dict[str, str] = dict(_fact(spec, "source_map", {}))

    # 落地目录里可能混着多个批次，只取最新一周。
    p.add("FilterWhereMaxMin", main, column="DATE_WEEK", expression="max", condition="==")

    # 自由文本列先按列兜字符串形式的空值，再统一 FillNA。
    for src in source_map:
        if src.startswith(_TEXT_PLACEHOLDER_PREFIXES):
            p.add("AddColumns", main, columns=[src], value=_null_literal(src, "Unassigned"))

    if source_map:
        p.add("RenameColumns", main, mapColumns=source_map)

    if dup_keys:
        partition = ", ".join(dup_keys)
        p.add(
            "AddColumns",
            main,
            columns=["DUP_REC_CNT"],
            value=_expr(f"CAST(COUNT(*) OVER (PARTITION BY {partition}) AS INT)"),
        )

    # 历史批和增量批共用一个分区，靠文件名区分来源。
    p.add(
        "AddColumns",
        main,
        columns=["SOURCE_FILE"],
        value=_expr(
            "concat('INTENT_BOMBORA_', "
            "date_format(current_timestamp(), 'yyyy-MM-dd HH:mm:ss'), '.csv')"
        ),
    )

    # 一行事实在候选展开后会被复制多份，必须先有一个稳定的行号才能收敛回一行。
    p.add("UniqueID", main, columns=["BOMBORA_ROW_ID"], mode="overwrite")


# ---------------------------------------------------------------- 阶段三：公司关联
def _link_company(p: _Plan, spec: DesignSpec, main: str) -> None:
    """文档里的 Company Linkage Specification，逐条落成变换。

    顺序不能换：
        候选数 -> join 候选 -> 打分 -> 排序 -> 取第一 -> 套基数规则
    先打分再算候选数，候选数会被 join 出来的行数污染。
    """
    if not _has(spec, "LOCATION_COMPANY_V2_DIMENSION"):
        return

    lc = _alias_of(spec, "LOCATION_COMPANY_V2_DIMENSION")
    duns = _alias_of(spec, "DIM_DUNS")
    d = _DIM_COLUMNS
    counts = "LOCATION_COMPANY_DOMAIN_COUNTS"

    has_country = _has(spec, "COUNTRY_CODE_REF.CSV")
    has_duns = _has(spec, "DIM_DUNS")

    company_cols = [
        f"{main}.*",
        f"{lc}.{d['company_key']}",
        f"{lc}.{d['company_duns']}",
    ]
    if has_country:
        company_cols.append(f"{lc}.COMPANY_LANDED_COUNTRY_NAME")

    p.add(
        "JoinByExpression",
        [main, lc],
        joinExpression=(
            f"lower(trim({main}.INTENT_DOMAIN_NAME)) = "
            f"lower(trim({lc}.{d['company_domain']})) AND "
            f"{lc}.{d['company_key']} > 0"
        ),
        joinType="left",
        columnsToSelect=company_cols,
    )

    if has_duns:
        p.add(
            "JoinByExpression",
            [main, duns],
            joinExpression=(
                f"{main}.{d['company_duns']} = {duns}.{d['duns_key']}"
            ),
            joinType="left",
            columnsToSelect=[
                f"{main}.*",
                f"{duns}.{d['duns_postal']}",
                f"{duns}.{d['duns_phone']}",
            ],
        )

    # 缺 DUNS 维表时邮编项恒为 0，但不能引用一个不存在的列——
    # 那会让整段 SQL 在运行期才报错，而不是在生成期。
    if has_duns:
        postal = _norm("HQ_Zip")
        candidate_postal = _norm(d["duns_postal"])
        postal_value = _expr(
            f"CASE WHEN {postal} <> '' AND {postal} = {candidate_postal} "
            "THEN 1 ELSE 0 END"
        )
    else:
        postal_value = "lit(0)"
    p.add("AddColumns", main, columns=["POSTAL_MATCH_SCORE"], value=postal_value)

    # 源里没有公司电话，这一项恒为 0。留字段不留逻辑，
    # 是因为后面的分数是三项相加，少一项会让权重悄悄变成 1/2。
    p.add("AddColumns", main, columns=["PHONE_MATCH_SCORE"], value="lit(0)")

    if has_country:
        country = _norm("COMPANY_COUNTRY_NAME")
        landed = _norm("COMPANY_LANDED_COUNTRY_NAME")
        country_value = _expr(
            f"CASE WHEN {country} <> '' AND {country} = {landed} THEN 1 ELSE 0 END"
        )
    else:
        country_value = "lit(0)"
    p.add("AddColumns", main, columns=["COUNTRY_MATCH_SCORE"], value=country_value)
    p.add(
        "AddColumns",
        main,
        columns=["LOCATION_MATCH_SCORE"],
        value=_expr(
            "POSTAL_MATCH_SCORE + PHONE_MATCH_SCORE + COUNTRY_MATCH_SCORE"
        ),
    )
    p.add(
        "AddColumns",
        main,
        columns=["LOCATION_MATCH_RANK"],
        value=_expr(
            "ROW_NUMBER() OVER (PARTITION BY BOMBORA_ROW_ID "
            f"ORDER BY LOCATION_MATCH_SCORE DESC, {d['company_key']} ASC)"
        ),
    )
    p.add("FilterData", main, whereClause="LOCATION_MATCH_RANK = 1")

    p.add(
        "JoinByExpression",
        [main, counts],
        joinExpression=(
            f"lower(trim({main}.INTENT_DOMAIN_NAME)) = {counts}.LOCATION_MATCH_DOMAIN"
        ),
        joinType="left",
        columnsToSelect=[
            f"{main}.*",
            f"coalesce({counts}.LOCATION_CANDIDATE_COUNT, 0) AS LOCATION_CANDIDATE_COUNT",
        ],
    )

    # 基数规则：唯一候选直接接受；多候选要求分数大于 0；否则 -1 并保留记录。
    p.add(
        "AddColumns",
        main,
        columns=[d["company_key"]],
        value=_expr(
            "CASE WHEN LOCATION_CANDIDATE_COUNT = 1 "
            "OR (LOCATION_CANDIDATE_COUNT > 1 AND LOCATION_MATCH_SCORE > 0) "
            f"THEN coalesce({d['company_key']}, -1) ELSE -1 END"
        ),
    )
    helpers = [
        "BOMBORA_ROW_ID",
        "LOCATION_CANDIDATE_COUNT",
        "LOCATION_MATCH_RANK",
        "POSTAL_MATCH_SCORE",
        "PHONE_MATCH_SCORE",
        "COUNTRY_MATCH_SCORE",
        "LOCATION_MATCH_SCORE",
    ]
    if has_country:
        helpers.append("COMPANY_LANDED_COUNTRY_NAME")
    p.add("Drop", main, columns=helpers)
    p.add(
        "RenameColumns",
        main,
        mapColumns={d["company_key"]: "LOCATION_COMPANY_KEY"},
    )


# ---------------------------------------------------------------- 阶段四：意图分类
def _map_intent(p: _Plan, spec: DesignSpec, main: str) -> None:
    d = _DIM_COLUMNS
    ut = _alias_of(spec, "MARKETING_INTENT_UT_MAPPING_DIMENSION")

    p.add(
        "AddColumns",
        main,
        columns=["INTENT_CATEGORY"],
        value=_invalid_literal(
            "INTENT_CATEGORY", "Unassigned", ("", "NONE", "NAN", "NULL", "UNASSIGNED")
        ),
    )
    p.add(
        "AddColumns",
        main,
        columns=["INTENT_TOPIC_NAME"],
        value=_invalid_literal(
            "INTENT_TOPIC_NAME", "Unassigned", ("", "NONE", "NAN", "NULL", "UNASSIGNED")
        ),
    )

    if not _has(spec, "MARKETING_INTENT_UT_MAPPING_DIMENSION"):
        return

    p.add(
        "JoinByExpression",
        [main, ut],
        joinExpression=(
            f"{main}.INTENT_CATEGORY = {ut}.{d['ut_category']} AND "
            f"{main}.INTENT_TOPIC_NAME = {ut}.{d['ut_topic']}"
        ),
        joinType="left",
        columnsToSelect=[
            f"{main}.*",
            f"{ut}.{d['ut_code']} AS UT_CD",
            f"{ut}.{d['ut_mapping_key']} AS UT_MAPPING_KEY",
            f"{ut}.{d['ut_status']}",
        ],
    )

    p.add(
        "AddColumns",
        main,
        columns=["INTENT_STATUS"],
        value=_expr(
            "CASE WHEN UT_CD IS NULL OR upper(trim(UT_CD)) "
            "IN ('', 'NULL', 'NAN', 'NOMAP') THEN 'Inactive' ELSE 'Active' END"
        ),
    )
    p.add("Drop", main, columns=[d["ut_status"]])


# ---------------------------------------------------------------- 阶段五：日期与键
def _keys_and_dates(p: _Plan, spec: DesignSpec, main: str, dup_keys: list[str]) -> None:
    d = _DIM_COLUMNS
    period = _alias_of(spec, "PERIOD_V4_DIMENSION_GREGORIAN")

    if _has(spec, "PERIOD_V4_DIMENSION_GREGORIAN"):
        p.add(
            "JoinByExpression",
            [main, period],
            joinExpression=(
                f"to_timestamp({main}.ACTVTY_DT, 'yyyyMMdd') = {period}.{d['period_date']}"
            ),
            joinType="left",
            columnsToSelect=[
                f"{main}.*",
                f"{period}.{d['period_key']} AS ACTIVITY_DATE_KEY",
            ],
        )
        p.add(
            "JoinByExpression",
            [main, period],
            joinExpression=(
                f"current_date() = to_date({period}.{d['period_date']})"
            ),
            joinType="left",
            columnsToSelect=[
                f"{main}.*",
                f"{period}.{d['period_key']} AS IMPORT_DATE_KEY",
            ],
        )

    p.add(
        "AddColumns",
        main,
        columns=["PERSISTENT_ID"],
        value=_expr(
            "md5(concat("
            "coalesce(cast(INTENT_COMPANY_NAME AS string), '__NULL__'), '|', "
            "coalesce(cast(INTENT_DOMAIN_NAME AS string), '__NULL__'), '|', "
            "coalesce(cast(METRO_AREA_INTENT_CMPSIT_SCORE AS string), '__NULL__'), '|', "
            "coalesce(cast(METRO_AREA_BUCKET_CD AS string), '__NULL__'), '|', "
            "coalesce(cast(POSTAL_CD_LIST AS string), '__NULL__'), '|', "
            "coalesce(date_format(ACTVTY_DT, 'yyyy-MM-dd'), '__NULL__')"
            "))"
        ),
    )
    p.add(
        "AddColumns",
        main,
        columns=["ACTIVITY_DATE_SORT"],
        value=_expr("to_date(ACTVTY_DT, 'yyyyMMdd')"),
    )
    p.add(
        "SparkSql",
        main,
        alias="BOMBORA_MAX_ACTIVITY_DATE_DF",
        query=(
            "SELECT max(ACTIVITY_DATE_SORT) AS MAX_ACTIVITY_DATE "
            f"FROM {main}"
        ),
    )
    p.add("Drop", main, columns=["DATE_WEEK"])
    p.add(
        "SparkSql",
        [main, "BOMBORA_MAX_ACTIVITY_DATE_DF"],
        query=(
            "SELECT b.*, concat(date_format(m.MAX_ACTIVITY_DATE, 'yyyy'), "
            "lpad(weekofyear(m.MAX_ACTIVITY_DATE), 2, '0')) AS DATE_WEEK "
            f"FROM {main} b CROSS JOIN BOMBORA_MAX_ACTIVITY_DATE_DF m"
        ),
    )

    if dup_keys:
        # 同一组重复记录只保留最新一条，计数留在保留行上。
        p.add(
            "AddWindowColumn",
            main,
            targetColumnName="DUP_REC_RANK",
            partitionByColumns=dup_keys,
            orderByColumns=["ACTVTY_DT"],
            functionName="ROW_NUMBER",
            descendingOrder=True,
        )
        p.add("FilterData", main, whereClause="DUP_REC_RANK = 1")
        p.add("Drop", main, columns=["DUP_REC_RANK", "ACTIVITY_DATE_SORT"])


# ---------------------------------------------------------------- 阶段六：国家键
def _resolve_country(p: _Plan, spec: DesignSpec, main: str) -> None:
    cc = _alias_of(spec, "COUNTRY_CODE_REF.CSV")
    geo = _alias_of(spec, "GEOGRAPHY_V3_DIMENSION")

    if _has(spec, "COUNTRY_CODE_REF.CSV"):
        p.add(
            "JoinByExpression",
            [main, cc],
            joinExpression=(
                f"upper(trim({main}.COMPANY_COUNTRY_NAME)) = {cc}.CTRY_NAME"
            ),
            joinType="left",
            columnsToSelect=[f"{main}.*", f"{cc}.CTRY AS COUNTRY_CODE"],
        )
    if _has(spec, "GEOGRAPHY_V3_DIMENSION"):
        p.add(
            "JoinByExpression",
            [main, geo],
            joinExpression=f"{main}.COUNTRY_CODE = {geo}.COUNTRY_CODE",
            joinType="left",
            columnsToSelect=[f"{main}.*", f"{geo}.COMPANY_COUNTRY_KEY"],
        )
    if _has(spec, "COUNTRY_CODE_REF.CSV"):
        p.add("Drop", main, columns=["COUNTRY_CODE"])


# ---------------------------------------------------------------- 阶段七：收尾
def _finalize(p: _Plan, spec: DesignSpec, main: str, columns: tuple[SpecColumn, ...]) -> None:
    strings = [
        c.name
        for c in columns
        if c.data_type == "StringType" and c.name != "DATE_WEEK"
    ]
    ints = [c.name for c in columns if c.data_type in {"IntegerType", "LongType"}]

    p.add("CurrentDate", main, columnName="AUDIT_TIMESTAMP", level="now")
    p.add("UniqueID", main, columns=["SURROGATE_KEY"], mode="overwrite", sequential=True)
    p.add(
        "AddColumns",
        main,
        columns=["UT_CD"],
        value=_invalid_literal("UT_CD", "Unassigned", ("", "NULL", "NAN", "NOMAP")),
    )

    if strings:
        fill_strings = strings if "UT_CD" in strings else [*strings, "UT_CD"]
        p.add("FillNA", main, columns=fill_strings, value="Unassigned")
        # FillNA 不处理空串，FillEmpty 补一刀。两个都留，是因为
        # 只留一个的时候，另一种空值会原样落进 parquet。
        p.add("FillEmpty", main, columns=fill_strings, value="Unassigned")
    if ints:
        p.add("FillNA", main, columns=ints, value=-1)

    p.add("Select", main, columns=[c.name for c in columns])


# ---------------------------------------------------------------- 阶段八：历史 + 增量
def _dup_job(
    spec: DesignSpec,
    job_type: str,
    columns: tuple[SpecColumn, ...],
    dup_keys: list[str],
    load_path: str,
) -> dict[str, Any]:
    """历史批 + 增量批合并。

    合并规则不是 union 就完事：
        1. 增量里出现过的键，历史里的旧行要删掉（否则同一条事实出现两次）
        2. 重复计数要相加，不能取增量那一份（否则历史里的重复数丢了）
    """
    main_alias = "BOMBORA_HIST_DF"
    delta = "BOMBORA_DELTA_DF"
    merged = "BOMBORA_MERGED_DF"

    p = _Plan()
    for alias in (main_alias, delta):
        p.add("Cast", alias, columns=["DATE_WEEK"], columnsType="string")
    p.add(
        "SparkSql",
        main_alias,
        query=(
            f"SELECT history.* FROM {main_alias} history INNER JOIN ("
            " SELECT DISTINCT CAST(DATE_WEEK AS INT) AS DATE_WEEK"
            f" FROM {main_alias}"
            " WHERE CAST(DATE_WEEK AS INT) IS NOT NULL"
            " ORDER BY DATE_WEEK DESC LIMIT 10"
            ") weeks ON CAST(history.DATE_WEEK AS INT) = weeks.DATE_WEEK"
        ),
    )

    keys = dup_keys or []
    if keys:
        join_expr = " AND ".join(f"{delta}.{k} <=> {main_alias}.{k}" for k in keys)
        p.add(
            "JoinByExpression",
            [delta, main_alias],
            joinExpression=join_expr,
            joinType="left",
            columnsToSelect=[
                f"{delta}.*",
                f"{main_alias}.DUP_REC_CNT AS HIST_DUP_REC_CNT",
            ],
        )
        p.add(
            "AddColumns",
            delta,
            columns=["DUP_REC_CNT"],
            value=_expr(
                "CAST(coalesce(DUP_REC_CNT, 0) + coalesce(HIST_DUP_REC_CNT, 0) AS INT)"
            ),
        )
        p.add("Drop", delta, columns=["HIST_DUP_REC_CNT"])
        anti = " AND ".join(
            f"history.{k} <=> delta.{k}" for k in keys
        )
        p.add(
            "SparkSql",
            [main_alias, delta],
            query=(
                f"SELECT history.* FROM {main_alias} history "
                f"LEFT ANTI JOIN {delta} delta ON {anti}"
            ),
        )

    select_cols = [c.name for c in columns]
    p.add("Select", main_alias, columns=select_cols)
    p.add("Select", delta, columns=select_cols)
    p.add("Union", [delta, main_alias], alias=merged)
    p.add("CreateCheckpoint", merged, clearCache=True)

    extracts = [
        _extract(
            main_alias,
            "cos",
            "epm-marketing-cos",
            False,
            {
                "alias": "MARKETING",
                "countRows": False,
                "basePath": _fact(spec, "hist_base_path", ""),
                "query": "/DATE_WEEK=*/*",
            },
        ),
        _extract(
            delta,
            "cos",
            "epm-marketing-cos",
            True,
            {
                "alias": "MARKETING",
                "countRows": False,
                "basePath": f"{load_path}_CURRENT/transform.parquet/",
                "query": "/*",
            },
        ),
    ]

    load = {
        "metadata": {
            "name": _fact(spec, "table_name", "FACT_TABLE"),
            "documentation": _fact(spec, "documentation", ""),
        },
        "schema": _schema(columns),
        "connections": [
            {
                "credentials": "epm-marketing-cos",
                "connector": "cos",
                "alias": "MARKETING",
                "countRows": False,
                "loadObject": merged,
                "numPartitions": 2,
                "partitionBy": ["DATE_WEEK"],
                "query": f"{load_path}/transform.parquet",
                "mode": "overwrite",
            }
        ],
    }

    return _job(
        f"{job_type}-dup",
        "M",
        extracts,
        p.items,
        load,
        spark_config={"spark.sql.sources.partitionOverwriteMode": "static"},
    )


def compile_spec(spec: DesignSpec) -> tuple[str, dict[str, Any]] | None:
    """认识的模板家族：需求 -> YAML 文本（不调模型）。

    返回 (文本, 统计)。不认识的家族返回 None，调用方退回模型。
    统计里只放"能直接展示给人"的数字，不放中间结构——
    中间结构是计划器的实现细节，接口层不该依赖它。
    """
    from pdc import emit

    built = plan(spec)
    if built is None:
        return None
    jobs = built["spec"]["jobs"]
    stats = {
        "engine": "plan",
        "jobs": len(jobs),
        "extracts": sum(len(j["extract"]) for j in jobs),
        "transforms": sum(len(j["transform"]) for j in jobs),
    }
    return emit.dump(built), stats


# ---------------------------------------------------------------- 入口
def plan(spec: DesignSpec) -> dict[str, Any] | None:
    """认识这个模板家族就返回完整计划，不认识返回 None（调用方退回模型）。"""
    if spec.profile != "marketing_intent":
        return None

    job_type = str(_fact(spec, "job_type", "eid"))
    main = str(_fact(spec, "main_alias", "BOMBORA_DF"))
    load_path = str(_fact(spec, "load_path", "fact/FACT_TABLE"))
    table_name = str(_fact(spec, "table_name", "FACT_TABLE"))
    columns = spec.columns_of(job_type)
    dup_keys = [str(k) for k in _fact(spec, "dup_keys", [])]
    has_dup = bool(_fact(spec, "has_hist_delta", False))

    p = _Plan()
    _prep_dimensions(p, spec)
    _prep_main(p, spec, main, dup_keys)
    _link_company(p, spec, main)
    _map_intent(p, spec, main)
    _keys_and_dates(p, spec, main, dup_keys)
    _resolve_country(p, spec, main)
    _finalize(p, spec, main, columns)

    extracts: list[dict[str, Any]] = []
    for e in spec.extracts:
        if e.job.endswith("-dup"):
            continue
        if e.alias == main:
            extracts.append(
                _extract(
                    main,
                    "cos",
                    "cos",
                    True,
                    {
                        "alias": "MARKETING",
                        "basePath": _fact(spec, "main_base_path", ""),
                        "query": "/DATE_WEEK=*/*",
                    },
                )
            )
            continue
        layout = _SOURCE_LAYOUT.get(e.alias)
        if not layout:
            # 家族里没见过这个源。不能静默丢掉——丢一个源，后面的 join 就落空。
            # 按通用形状保留，路径和连接器沿用解析器的推断。
            extracts.append(
                _extract(
                    e.alias,
                    e.connector or "cos",
                    e.credentials or e.connector or "cos",
                    False,
                    {"query": e.query},
                )
            )
            continue
        alias = str(layout["alias"])
        conn: dict[str, Any] = {}
        if layout.get("plugin"):
            conn["plugin"] = layout["plugin"]
        if layout.get("root_folder_id"):
            conn["root_folder_id"] = layout["root_folder_id"]
        if layout.get("path"):
            conn["path"] = layout["path"]
        if layout.get("file_name"):
            conn["file_name"] = layout["file_name"]
        elif layout["connector"] == "box":
            conn["file_name"] = e.query
        if layout.get("query"):
            conn["query"] = layout["query"]
        elif layout["connector"] != "box":
            # skapi 和 cos 都要一个定位字段。box 用 file_name，不用 query。
            conn["query"] = e.query
        if layout.get("conn_alias"):
            conn["alias"] = layout["conn_alias"]
        if layout.get("key_name"):
            conn["keyName"] = layout["key_name"]
        if layout.get("natural_key"):
            conn["naturalKey"] = list(layout["natural_key"])
        if layout.get("request_flags"):
            conn["requestFlags"] = dict(layout["request_flags"])
        if layout["connector"] == "cos":
            conn.setdefault("countRows", False)
        extracts.append(
            _extract(
                alias,
                str(layout["connector"]),
                str(layout["credentials"]),
                e.alias == "COUNTRY_CODE_REF.CSV",
                conn,
            )
        )

    first_load_name = f"{table_name}_CURRENT" if has_dup else table_name
    load = {
        "metadata": {
            "name": first_load_name,
            "documentation": _fact(spec, "documentation", ""),
        },
        "schema": _schema(columns),
        "connections": [
            {
                "credentials": "epm-marketing-cos",
                "connector": "cos",
                "alias": "MARKETING",
                "countRows": False,
                "loadObject": main,
                "numPartitions": 2,
                "query": f"{load_path}_CURRENT/transform.parquet" if has_dup else f"{load_path}/transform.parquet",
                "mode": "overwrite",
            }
        ],
    }

    jobs = [
        _job(
            job_type,
            str(_fact(spec, "size", "M")),
            extracts,
            p.items,
            load,
        )
    ]
    if has_dup:
        jobs.append(_dup_job(spec, job_type, columns, dup_keys, load_path))

    return {
        "apiVersion": API_VERSION,
        "kind": spec.kind,
        "metadata": {"name": spec.pipeline},
        "spec": {
            "owner": {
                "name": str(_fact(spec, "owner_name", "")),
                "github": str(_fact(spec, "owner_github", "")),
                "contact": str(_fact(spec, "owner_contact", "")),
            },
            "jobs": jobs,
        },
    }
