from pathlib import Path

import pytest

from pdc.checks import dropped_and_renamed, schema_diff
from pdc.parser import PipelineParseError, parse_pipeline

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
SPEND = PIPELINES / "fact-order-fulfillment.yaml"
FORECAST = PIPELINES / "dim-customer-segment.yaml"
RULES = SAMPLES / "rules"

needs_repo = pytest.mark.skipif(not SPEND.exists(), reason="samples 缺失")


def test_rejects_non_pipeline(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("kind: ConfigMap\nmetadata:\n  name: nope\n", encoding="utf-8")
    with pytest.raises(PipelineParseError):
        parse_pipeline(f)


def test_rejects_broken_yaml(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("kind: FactPipeline\nspec: [unclosed\n", encoding="utf-8")
    with pytest.raises(PipelineParseError):
        parse_pipeline(f)


@needs_repo
def test_spend_details_shape():
    facts = parse_pipeline(SPEND)
    assert facts.name == "fact-order-fulfillment"
    assert [j.job_type for j in facts.jobs] == [
        "eid",
        "datamart-parquet",
        "datamart-self-serve",
    ]
    ############################################################
    # 这四个数字是和 YAML 手工核对过的，改代码后如果挂了
    # 说明你的解析逻辑变了 —— 这就是第一个回归测试
    ############################################################
    assert len(facts.jobs[0].extracts) == 4
    assert len(facts.jobs[0].transforms) == 11
    assert len(facts.jobs[0].load.schema) == 19
    assert len(facts.jobs[1].transforms) == 3
    assert len(facts.jobs[1].load.schema) == 11


@needs_repo
def test_main_df_detection():
    facts = parse_pipeline(SPEND)
    mains = [e.alias for e in facts.jobs[0].extracts if e.main_df]
    assert mains == ["ORDER_LINES"]


@needs_repo
def test_spend_details_schema_drift_is_detected():
    """真实缺陷：帧 31 列 vs schema 25 列，差的正好这 6 个。"""
    facts = parse_pipeline(SPEND)
    issues = schema_diff(facts, 0)
    assert issues, "应该检测到 schema 漂移"
    blob = " ".join(issues)
    for col in (
        "CUSTOMER_COUNTRY",
        "FISCAL_QUARTER",
        "FISCAL_YEAR",
        "UNIT_PRICE_USD",
        "GROSS_MARGIN_USD",
        "REGION_CODE",
        "CHANNEL",
    ):
        assert col in blob


@needs_repo
def test_missing_downstream_schema_is_reported_not_silent():
    """self-serve job 没有 schema 声明，不能假装通过比对。"""
    facts = parse_pipeline(SPEND)
    issues = schema_diff(facts, 1)
    assert len(issues) == 1
    assert "schema_undeclared" in issues[0]


@needs_repo
def test_forecast_source_has_no_schema_drift():
    facts = parse_pipeline(FORECAST)
    assert facts.jobs[0].job_type == "eid"
    assert schema_diff(facts, 0) == []


@needs_repo
def test_drop_and_rename_are_collected():
    facts = parse_pipeline(SPEND)
    dropped, renamed = dropped_and_renamed(facts.jobs[1])
    assert "WAREHOUSE_CODE" in dropped
    assert renamed["ORDER_FULFILLMENT_KEY"] == "SK_ORDER_FULFILLMENT"


def test_scalar_partition_by_is_one_name(tmp_path):
    """真实语料里有 `partitionBy: PARTKEY`。

    按字符串迭代会变成 P,A,R,T,K,E,Y——不报错，但分区键全错。
    这是跑 5,831 条真实语料时抓到的，12 条样例覆盖不到。
    """
    f = tmp_path / "scalar.yaml"
    f.write_text(
        "apiVersion: crd.ibm.k8s.io/v1\n"
        "kind: FactPipeline\n"
        "metadata: {name: scalar-partition}\n"
        "spec:\n"
        "  jobs:\n"
        "    - jobType: eid\n"
        "      setups: {size: T}\n"
        "      extract:\n"
        "        - alias: SRC\n"
        "          main_df: true\n"
        "          connections:\n"
        "            - connector: cos\n"
        "              credentials: cos\n"
        "              query: a/b\n"
        "              naturalKey: SRC_KEY\n"
        "      load:\n"
        "        connections:\n"
        "          - connector: cos\n"
        "            credentials: cos\n"
        "            query: a/b\n"
        "            partitionBy: PARTKEY\n",
        encoding="utf-8",
    )
    facts = parse_pipeline(f)
    assert facts.jobs[0].load.partition_by == ("PARTKEY",)
    assert facts.jobs[0].extracts[0].natural_key == ("SRC_KEY",)
