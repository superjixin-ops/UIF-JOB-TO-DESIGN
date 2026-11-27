"""模板家族的连接约定。

文档只写业务名（"join with `COUNTRY_CODE_REF.csv`"），
pipeline 里要写物理路径、别名、连接器、凭证。

这一层**推不出来**，只能人给；给一次之后整个家族复用。
所以它不放在解析器里，也不放在计划器里——两边都要用，
放中间才不会出现"解析器认一套别名、计划器认另一套"。

新增一个数据源时，这里加一条，并在接入环境里确认一次路径。
"""

from __future__ import annotations

from typing import Any

# 目标平台的 API 组。写死在这里而不是让模型猜：
# 模型猜出来的 crd.example.io/v1 能通过校验，但部署时会被拒。
API_VERSION = "crd.ibm.k8s.io/v1"

# 物理表 -> pipeline 内的写法。
MARKETING_INTENT: dict[str, dict[str, Any]] = {
    "COUNTRY_CODE_REF.CSV": {
        "alias": "INTENT_DATA_COUNTRY_CODE_REF",
        "connector": "box",
        "credentials": "box",
        "plugin": "uif_validations",
        # box 的根目录 id 是部署常量，换环境要改。
        "root_folder_id": "377095991711",
        "path": "/",
    },
    "GEOGRAPHY_V3_DIMENSION": {
        "alias": "DIMENSION_GEOGRAPHY",
        "connector": "skapi",
        "credentials": "skapi",
        "key_name": "SURROGATE_KEY",
        "natural_key": ["LEVEL", "CODE"],
        "request_flags": {"includeDeleted": False},
    },
    "LOCATION_COMPANY_V2_DIMENSION": {
        "alias": "LOCATION_COMPANY_V2_DIMENSION",
        "connector": "cos",
        "credentials": "cos",
        "conn_alias": "ENTERPRISE_DIMENSIONS",
        "query": "dimension/LOCATION_COMPANY_V2_DIMENSION/transform.parquet",
    },
    "DIM_DUNS": {
        "alias": "DIM_DUNS",
        "connector": "cos",
        "credentials": "cos",
        "conn_alias": "ENTERPRISE_DIMENSIONS",
        "query": "dimension/DIM_DUNS/transform.parquet",
    },
    "PERIOD_V4_DIMENSION_GREGORIAN": {
        "alias": "PERIOD_V4_DIMENSION_GREGORIAN",
        "connector": "cos",
        "credentials": "cos",
        "conn_alias": "ENTERPRISE_DIMENSIONS",
        "query": "dimension/PERIOD_V4_DIMENSION_GREGORIAN/transform.parquet",
    },
    "MARKETING_INTENT_UT_MAPPING_DIMENSION": {
        "alias": "DIM_MAPPING_UT",
        "connector": "cos",
        "credentials": "cos",
        "conn_alias": "MARKETING",
        "query": "dimension/MARKETING_INTENT_UT_MAPPING_DIMENSION/transform.parquet/*",
    },
}

# 计划中间产出的临时数据集。它们不是数据源，但会出现在 extract/aliasResult 里，
# 弱契约检查要认得这些名字，否则会把它们判成"编造的别名"。
DERIVED_ALIASES = (
    "BOMBORA_DF",
    "BOMBORA_HIST_DF",
    "BOMBORA_DELTA_DF",
    "BOMBORA_MERGED_DF",
    "BOMBORA_MAX_ACTIVITY_DATE_DF",
    "LOCATION_COMPANY_DOMAIN_COUNTS",
)


def allowed_aliases(family: dict[str, dict[str, Any]] = MARKETING_INTENT) -> tuple[str, ...]:
    """这个家族允许出现的全部数据源别名。"""
    return tuple(str(v["alias"]) for v in family.values()) + DERIVED_ALIASES


# ---------------------------------------------------------------- Join 方言
# 平台上有两套 Join，参数完全不同：
#
#   legacy    mapColumns + how + mergeColumns + renameColumns
#             5,831 条语料里 12,932 次，占 99.9%
#   current   joinMapping + left + right（官方文档要求的形状）
#             语料里 0%
#
# 生成时必须显式选一套。默认 legacy，因为集群上跑的是它；
# 升级平台后把这里改成 "current"，而不是让模型每次猜。
JOIN_DIALECT = "legacy"


def join_step(
    left: str,
    right: str,
    mapping: dict[str, str],
    *,
    alias: str,
    how: str = "left",
    merge_columns: list[str] | None = None,
    rename_columns: dict[str, str] | None = None,
    dialect: str | None = None,
) -> dict[str, Any]:
    """按选定的方言生成一个 Join 变换。

    legacy 的 mapColumns 是"右表列 -> 左表列"的映射；
    current 的 joinMapping 要求每列带表名限定，并且必须写 left/right。
    两种形状不能混，混了就是非法 YAML。
    """
    mode = dialect or JOIN_DIALECT
    if mode == "current":
        qualified = {
            f"{left}.{lcol}": f"{right}.{rcol}" for rcol, lcol in mapping.items()
        }
        return {
            "transformType": "Join",
            "objectNames": [left, right],
            "left": left,
            "right": right,
            "joinMapping": qualified,
            "how": how,
            "aliasResult": alias,
        }
    return {
        "transformType": "Join",
        "objectNames": [left, right],
        "how": how,
        "mapColumns": dict(mapping),
        "mergeColumns": list(merge_columns or []),
        "renameColumns": dict(rename_columns or {}),
        "aliasResult": alias,
    }
