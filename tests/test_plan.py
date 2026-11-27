"""计划器的测试。

为什么这些断言值得写：

    参考实现（手写）是 2 个 job、9 个数据源、71 个变换。
    生成器第一次做到这个规模时，任何一个数字对不上都说明
    某一段规则没生效——而这些规则**不会**在结构校验里报错：
    `Trim` 少一个 columns 是合法 YAML，`RenameColumns` 少 mapColumns 也是。

    所以这里断言的是"内容形状"，不是"能不能解析"。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest
import yaml

from pdc import emit
from pdc.design_reader import DesignSpec, SpecColumn, SpecExtract, SpecLoad
from pdc.import_marketing import _parse_dup_keys, _parse_landing, _read_column_meta
from pdc.parser import parse_pipeline
from pdc.plan import compile_spec, plan
from pdc.profiles import allowed_aliases
from pdc.spec_grounding import check_text

COLUMNS: tuple[tuple[str, str], ...] = (
    ("SURROGATE_KEY", "IntegerType"),
    ("SOURCE_FILE", "StringType"),
    ("INTENT_COMPANY_NAME", "StringType"),
    ("LOCATION_COMPANY_KEY", "IntegerType"),
    ("INTENT_DOMAIN_NAME", "StringType"),
    ("COMPANY_COUNTRY_NAME", "StringType"),
    ("COMPANY_COUNTRY_KEY", "IntegerType"),
    ("INTENT_CATEGORY", "StringType"),
    ("INTENT_TOPIC_NAME", "StringType"),
    ("UT_MAPPING_KEY", "IntegerType"),
    ("INTENT_STATUS", "StringType"),
    ("ACTIVITY_DATE_KEY", "IntegerType"),
    ("IMPORT_DATE_KEY", "IntegerType"),
    ("DATE_WEEK", "StringType"),
    ("COMP_SIZE_RNG", "StringType"),
    ("IND_NAME", "StringType"),
    ("INTENT_CMPSIT_SCORE", "IntegerType"),
    ("BUCKET_CD", "IntegerType"),
    ("METRO_AREA_NAME", "StringType"),
    ("METRO_AREA_INTENT_CMPSIT_SCORE", "IntegerType"),
    ("METRO_AREA_BUCKET_CD", "IntegerType"),
    ("POSTAL_CD_LIST", "IntegerType"),
    ("DUP_REC_CNT", "IntegerType"),
    ("HQ_CITY", "StringType"),
    ("AUDIT_TIMESTAMP", "TimestampType"),
    ("HQ_STATE", "StringType"),
    ("HQ_ADDRESS1", "StringType"),
    ("HQ_ADDRESS2", "StringType"),
    ("PERSISTENT_ID", "StringType"),
    ("HQ_Zip", "IntegerType"),
    ("DOMAIN_ORIGIN", "StringType"),
)

SOURCE_MAP = {
    "Company": "INTENT_COMPANY_NAME",
    "Domain": "INTENT_DOMAIN_NAME",
    "HQ Country": "COMPANY_COUNTRY_NAME",
    "Category": "INTENT_CATEGORY",
    "Topic": "INTENT_TOPIC_NAME",
    "Date Stamp": "ACTVTY_DT",
    "Size": "COMP_SIZE_RNG",
    "Industry": "IND_NAME",
    "Composite Score": "INTENT_CMPSIT_SCORE",
    "Bucket Code": "BUCKET_CD",
    "Metro Area": "METRO_AREA_NAME",
    "Metro Composite Score": "METRO_AREA_INTENT_CMPSIT_SCORE",
    "Metro Bucket Code": "METRO_AREA_BUCKET_CD",
    "Zip Code": "POSTAL_CD_LIST",
    "HQ City": "HQ_CITY",
    "HQ State": "HQ_STATE",
    "HQ Address 1": "HQ_ADDRESS1",
    "HQ Address 2": "HQ_ADDRESS2",
    "HQ Zip": "HQ_Zip",
    "Domain Origin": "DOMAIN_ORIGIN",
}

DUP_KEYS = (
    "INTENT_COMPANY_NAME",
    "INTENT_DOMAIN_NAME",
    "METRO_AREA_INTENT_CMPSIT_SCORE",
    "METRO_AREA_BUCKET_CD",
    "POSTAL_CD_LIST",
    "ACTVTY_DT",
)


def _extract(job: str, alias: str, connector: str, query: str, main: bool = False) -> SpecExtract:
    return SpecExtract(
        job=job,
        alias=alias,
        connector=connector,
        credentials=connector,
        query=query,
        main_df=main,
    )


def marketing_spec() -> DesignSpec:
    columns = tuple(SpecColumn(name=n, data_type=t) for n, t in COLUMNS)
    jobs = ("eid-sftp", "eid-sftp-dup")
    return DesignSpec(
        pipeline="fact-market-intent-bombora",
        kind="FactPipeline",
        jobs=jobs,
        job_sizes=((j, "M") for j in jobs),
        extracts=(
            _extract("eid-sftp", "BOMBORA_DF", "cos", "core", main=True),
            _extract("eid-sftp", "LOCATION_COMPANY_V2_DIMENSION", "cos", "lc"),
            _extract("eid-sftp", "COUNTRY_CODE_REF.CSV", "box", "COUNTRY_CODE_REF.CSV"),
            _extract("eid-sftp", "GEOGRAPHY_V3_DIMENSION", "cos", "geo"),
            _extract("eid-sftp", "MARKETING_INTENT_UT_MAPPING_DIMENSION", "cos", "ut"),
            _extract("eid-sftp", "PERIOD_V4_DIMENSION_GREGORIAN", "cos", "period"),
            _extract("eid-sftp", "DIM_DUNS", "cos", "duns"),
            _extract("eid-sftp-dup", "BOMBORA_HIST_DF", "cos", "hist"),
            _extract("eid-sftp-dup", "BOMBORA_DELTA_DF", "cos", "delta", main=True),
        ),
        loads=tuple(
            SpecLoad(
                job=j,
                object_name="FACT_MARKET_INTENT_BOMBORA",
                query="fact/FACT_MARKET_INTENT_BOMBORA",
                columns=columns,
            )
            for j in jobs
        ),
        weak=True,
        profile="marketing_intent",
        facts={
            "job_type": "eid-sftp",
            "main_alias": "BOMBORA_DF",
            "main_base_path": "LANDING_ZONE/fact_market_intent/Bombora/core/transform.parquet",
            "hist_base_path": "LANDING_ZONE/fact_market_intent/Bombora/historical/transform.parquet",
            "load_path": "fact/FACT_MARKET_INTENT_BOMBORA",
            "table_name": "FACT_MARKET_INTENT_BOMBORA",
            "documentation": "FACT MARKET INTENT BOMBORA in Eid",
            "source_map": SOURCE_MAP,
            "dup_keys": list(DUP_KEYS),
            "has_hist_delta": True,
            "null_rule": "string -> Unassigned, int -> -1",
            "size": "M",
            "allowed_aliases": allowed_aliases(),
        },
    )


@pytest.fixture(scope="module")
def built() -> dict:
    result = plan(marketing_spec())
    assert result is not None
    return result


def _transforms(built: dict, job_index: int) -> list[dict]:
    return built["spec"]["jobs"][job_index]["transform"]


class TestShape:
    def test_two_jobs(self, built: dict) -> None:
        assert [j["jobType"] for j in built["spec"]["jobs"]] == ["eid-sftp", "eid-sftp-dup"]

    def test_nine_extracts(self, built: dict) -> None:
        assert sum(len(j["extract"]) for j in built["spec"]["jobs"]) == 9

    def test_transform_count(self, built: dict) -> None:
        # 与手写实现对齐：60 + 11。
        assert [len(j["transform"]) for j in built["spec"]["jobs"]] == [60, 11]

    def test_transform_type_histogram(self, built: dict) -> None:
        got = Counter(
            t["transformType"] for j in built["spec"]["jobs"] for t in j["transform"]
        )
        assert got["AddColumns"] == 22
        assert got["JoinByExpression"] == 10
        assert got["Drop"] == 6
        assert got["SparkSql"] == 5
        assert got["FilterData"] == 4
        assert got["RenameColumns"] == 3
        assert got["Select"] == 3
        assert got["FilterType2"] == 3
        assert got["Union"] == 1
        assert got["CreateCheckpoint"] == 1

    def test_api_version(self, built: dict) -> None:
        assert built["apiVersion"] == "crd.ibm.k8s.io/v1"


class TestParams:
    def test_no_empty_column_lists(self, built: dict) -> None:
        """空参数是最隐蔽的错：YAML 合法，语义变成空操作。"""
        for job in built["spec"]["jobs"]:
            for t in job["transform"]:
                for key in ("columns", "mapColumns", "columnsToSelect"):
                    if key in t:
                        assert t[key], f"{t['transformType']}.{key} 是空的"

    def test_every_transform_has_alias_result(self, built: dict) -> None:
        for job in built["spec"]["jobs"]:
            for t in job["transform"]:
                assert t.get("aliasResult"), t["transformType"]

    def test_rename_happens_before_joins(self, built: dict) -> None:
        types = [t["transformType"] for t in _transforms(built, 0)]
        rename = types.index("RenameColumns")
        first_main_join = next(
            i
            for i, t in enumerate(_transforms(built, 0))
            if t["transformType"] == "JoinByExpression"
            and t["objectNames"][0] == "BOMBORA_DF"
        )
        assert rename < first_main_join


class TestContract:
    def test_load_schema_matches_doc_order(self, built: dict) -> None:
        schema = [c["columnName"] for c in built["spec"]["jobs"][0]["load"]["schema"]]
        assert schema == [n for n, _ in COLUMNS]

    def test_final_select_is_exactly_the_schema(self, built: dict) -> None:
        select = _transforms(built, 0)[-1]
        assert select["transformType"] == "Select"
        assert select["columns"] == [n for n, _ in COLUMNS]

    def test_second_job_selects_the_same_columns(self, built: dict) -> None:
        selects = [t for t in _transforms(built, 1) if t["transformType"] == "Select"]
        assert len(selects) == 2
        assert selects[0]["columns"] == selects[1]["columns"]

    def test_load_paths(self, built: dict) -> None:
        first = built["spec"]["jobs"][0]["load"]["connections"][0]
        second = built["spec"]["jobs"][1]["load"]["connections"][0]
        assert first["query"] == "fact/FACT_MARKET_INTENT_BOMBORA_CURRENT/transform.parquet"
        assert second["query"] == "fact/FACT_MARKET_INTENT_BOMBORA/transform.parquet"
        assert second["partitionBy"] == ["DATE_WEEK"]


class TestEmit:
    def test_roundtrip(self, built: dict, tmp_path) -> None:
        target = emit.write(built, tmp_path / "pipeline.yaml")
        text = target.read_text(encoding="utf-8")
        assert yaml.safe_load(text) == built
        facts = parse_pipeline(target)
        assert len(facts.jobs) == 2

    def test_grounding_passes(self, built: dict, tmp_path) -> None:
        target = emit.write(built, tmp_path / "pipeline.yaml")
        spec = marketing_spec()
        violations = check_text(spec.to_prompt(), parse_pipeline(target), allowed_aliases())
        assert violations == []

    def test_unknown_profile_returns_none(self) -> None:
        spec = marketing_spec()
        assert plan(DesignSpec(**{**spec.__dict__, "profile": ""})) is None


class TestCatalogChoice:
    def test_dedup_follows_catalog(self) -> None:
        """去重写法不是写死的：字典说 DropDupes 更常见就用它。"""
        catalog = {
            "transforms": {
                "SelectDistinct": {"count": 10},
                "DropDupes": {"count": 100},
            }
        }
        built = plan(marketing_spec(), catalog)
        assert built is not None
        types = [t["transformType"] for t in built["spec"]["jobs"][0]["transform"]]
        assert "DropDupes" in types
        assert "SelectDistinct" not in types
        # 参数形状跟着变，不是只换名字
        first = next(t for t in built["spec"]["jobs"][0]["transform"] if t["transformType"] == "DropDupes")
        assert "subsetColumnsList" in first
        assert "columns" not in first

    def test_without_catalog_uses_default(self) -> None:
        built = plan(marketing_spec())
        types = [t["transformType"] for t in built["spec"]["jobs"][0]["transform"]]
        assert "SelectDistinct" in types
        assert "DropDupes" not in types


class TestOptionalSources:
    """维表可能缺席。规则要跟着缺，而不是引用一个不存在的列。"""

    def test_without_history_job(self) -> None:
        spec = replace(
            marketing_spec(),
            jobs=("eid-sftp",),
            extracts=tuple(e for e in marketing_spec().extracts if e.job == "eid-sftp"),
            facts={**marketing_spec().facts, "has_hist_delta": False},
        )
        built = plan(spec)
        assert built is not None
        assert [j["jobType"] for j in built["spec"]["jobs"]] == ["eid-sftp"]
        types = [t["transformType"] for t in built["spec"]["jobs"][0]["transform"]]
        assert "Union" not in types
        conn = built["spec"]["jobs"][0]["load"]["connections"][0]
        assert conn["query"] == "fact/FACT_MARKET_INTENT_BOMBORA/transform.parquet"

    def test_without_duns_still_scores(self) -> None:
        spec = replace(
            marketing_spec(),
            extracts=tuple(
                e for e in marketing_spec().extracts if e.alias != "DIM_DUNS"
            ),
        )
        built = plan(spec)
        assert built is not None
        transforms = built["spec"]["jobs"][0]["transform"]
        assert all("DIM_DUNS" not in t["objectNames"] for t in transforms)
        postal = next(t for t in transforms if t.get("columns") == ["POSTAL_MATCH_SCORE"])
        assert postal["value"] == "lit(0)"
        assert "POSTAL_CODE" not in str(transforms)


class TestCompile:
    def test_compile_returns_text_and_stats(self) -> None:
        text, stats = compile_spec(marketing_spec())
        assert stats["engine"] == "plan"
        assert stats["jobs"] == 2
        assert stats["extracts"] == 9
        assert stats["transforms"] == 71
        assert "filled" in stats
        assert "catalog_issues" in stats
        assert "apiVersion: crd.ibm.k8s.io/v1" in text

    def test_compile_returns_none_for_unknown_family(self) -> None:
        spec = marketing_spec()
        assert compile_spec(DesignSpec(**{**spec.__dict__, "profile": ""})) is None


class TestDocumentParsing:
    def test_landing_strips_bucket_and_partition(self) -> None:
        text = (
            "core: /epm-marketing-*-2/LANDING_ZONE/fact/Bombora/core/transform.parquet"
            "/date_week=XXXXXX/xxxxx.parquet\n"
            "historical: /epm-marketing-*-2/LANDING_ZONE/fact/Bombora/historical"
            "/transform.parquet/date_week=XXXXXX/xxxxx.parquet"
        )
        got = _parse_landing(text)
        assert got["core"] == "LANDING_ZONE/fact/Bombora/core/transform.parquet"
        assert got["historical"] == "LANDING_ZONE/fact/Bombora/historical/transform.parquet"

    def test_dup_keys_map_source_to_target(self) -> None:
        note = "identical: `COMPANY`, `DOMAIN`, `Zip Code`."
        assert _parse_dup_keys(note, SOURCE_MAP) == [
            "INTENT_COMPANY_NAME",
            "INTENT_DOMAIN_NAME",
            "POSTAL_CD_LIST",
        ]

    def test_source_column_offset_stops_at_computed_column(self) -> None:
        """计算列不认领源列名，否则后面整列错位一格。"""
        header = ["Target Column", "Json Source Columns", "Transformation 1", "Transformation 2"]
        idx = {h: i for i, h in enumerate(header)}
        rows = [
            tuple(header),
            ("INTENT_COMPANY_NAME", "Company", "Simple Movement", "rename"),
            # 这一行的 Domain 属于下一行，它是计算列，不该认领
            ("LOCATION_COMPANY_KEY", "Domain", "", "Company Linkage Specification"),
            ("INTENT_DOMAIN_NAME", None, "Simple Movement", "rename"),
        ]
        mapping, _ = _read_column_meta(rows, idx)
        assert mapping["Company"] == "INTENT_COMPANY_NAME"
        assert mapping["Domain"] == "INTENT_DOMAIN_NAME"
        assert "LOCATION_COMPANY_KEY" not in mapping.values()
