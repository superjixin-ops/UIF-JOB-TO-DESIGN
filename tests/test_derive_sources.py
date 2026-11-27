"""数据源推导器的测试。

为什么需要它：

    设计文档里 "join with COUNTRY_CODE_REF.csv" 这种句子，
    既是变换说明，也是数据源声明。
    漏掉一条，后面就少一个 extract，join 无从谈起。

    实测：靠这套规则，参考文件的 9 个数据源全部命中。
"""

from __future__ import annotations

from pdc.derive_sources import derive_sources, infer_connector, normalize_table


class TestNormalizeTable:
    def test_plain_table(self) -> None:
        assert normalize_table("DIM_DUNS") == "DIM_DUNS"

    def test_table_with_column(self) -> None:
        # 右边是列名，左边才是表
        assert normalize_table("PERIOD_V4_DIMENSION_GREGORIAN.DATE.") == (
            "PERIOD_V4_DIMENSION_GREGORIAN"
        )
        assert normalize_table("LOCATION_COMPANY_V2_DIMENSION.COMPANY_DOMAIN") == (
            "LOCATION_COMPANY_V2_DIMENSION"
        )

    def test_schema_prefix(self) -> None:
        # 右边是表名，左边是 schema
        assert normalize_table("epm.dim_geography") == "GEOGRAPHY_V3_DIMENSION"

    def test_csv_is_a_file(self) -> None:
        assert normalize_table("COUNTRY_CODE_REF.csv") == "COUNTRY_CODE_REF.CSV"


class TestInferConnector:
    def test_csv_goes_to_box(self) -> None:
        assert infer_connector("COUNTRY_CODE_REF.CSV") == "box"

    def test_others_go_to_cos(self) -> None:
        assert infer_connector("DIM_DUNS") == "cos"


class TestDeriveSources:
    def test_finds_join_lookup_merge(self) -> None:
        texts = [
            "First join with `COUNTRY_CODE_REF.csv` on ctry_name.",
            "Merge with DIM_MARKETING_INTENT_UT_MAPPING on topic.",
            "Convert to timestamp, then perform lookup against PERIOD_V4_DIMENSION_GREGORIAN.DATE",
        ]
        got = {s.table for s in derive_sources(texts)}
        assert "COUNTRY_CODE_REF.CSV" in got
        assert "MARKETING_INTENT_UT_MAPPING_DIMENSION" in got
        assert "PERIOD_V4_DIMENSION_GREGORIAN" in got

    def test_keyword_fallback_for_duns(self) -> None:
        # 文档里只写了 "DUNS postal code"，没写表名
        got = {s.table for s in derive_sources(["equals the candidate's DUNS postal code"])}
        assert "DIM_DUNS" in got

    def test_historical_and_delta_split_a_second_job(self) -> None:
        texts = [
            "For historical data, assign the value as X.",
            "For delta data, apply the following logic.",
        ]
        got = derive_sources(texts)
        jobs = {s.job for s in got}
        assert "eid-sftp-dup" in jobs
        assert {s.alias for s in got} == {"BOMBORA_HIST_DF", "BOMBORA_DELTA_DF"}

    def test_landing_becomes_main_source(self) -> None:
        got = derive_sources([], "core: /LANDING_ZONE/fact/transform.parquet")
        assert len(got) == 1
        assert got[0].alias == "BOMBORA_DF"
        assert got[0].connector == "cos"
