"""对抗测试：故意喂"坏输入"，验证系统不崩、不静默通过。

这一类测试在生产里叫 negative testing / robustness testing。
面试被问"你怎么保证不会挂"，答案不是"我写了很多 if"，
而是"我有一组对抗用例，每次改代码都跑"。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from pdc.design import build_design
from pdc.parser import PipelineParseError, parse_pipeline
from pdc.render import render_xlsx
from pdc.validate import validate_doc

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
PIPELINES = SAMPLES / "pipelines"
CRD = PIPELINES


def _write(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "p.yaml"
    f.write_text(body, encoding="utf-8")
    return f


# ---------------------------------------------------------------- 畸形输入
def test_empty_file(tmp_path):
    with pytest.raises(PipelineParseError):
        parse_pipeline(_write(tmp_path, ""))


def test_yaml_but_not_pipeline(tmp_path):
    with pytest.raises(PipelineParseError):
        parse_pipeline(_write(tmp_path, "kind: ConfigMap\nmetadata:\n  name: x\n"))


def test_no_jobs(tmp_path):
    f = _write(tmp_path, "kind: FactPipeline\nmetadata:\n  name: x\nspec:\n  jobs: []\n")
    facts = parse_pipeline(f)
    assert facts.jobs == ()
    doc = build_design(facts)          # 不能抛
    assert doc.sheets                  # 至少要出 Overview


def test_job_without_extract_or_load(tmp_path):
    f = _write(tmp_path, """
kind: FactPipeline
metadata: {name: x}
spec:
  jobs:
    - jobType: eid
""")
    facts = parse_pipeline(f)
    assert facts.jobs[0].extracts == ()
    assert facts.jobs[0].load is None
    build_design(facts)                # 不能抛


def test_load_without_connections(tmp_path):
    f = _write(tmp_path, """
kind: FactPipeline
metadata: {name: x}
spec:
  jobs:
    - jobType: eid
      load:
        metadata: {name: T}
""")
    facts = parse_pipeline(f)
    assert facts.jobs[0].load is None


def test_duplicate_columns_are_kept_verbatim(tmp_path):
    """重复列是真实存在的脏数据，不能去重（去重会掩盖问题）。"""
    f = _write(tmp_path, """
kind: FactPipeline
metadata: {name: x}
spec:
  jobs:
    - jobType: eid
      extract: [{alias: A, connections: [{connector: cos, query: q}]}]
      load:
        metadata: {name: T}
        connections: [{connector: cos, query: out}]
        schema:
          - {columnName: COL_A, dataType: IntegerType, nullAble: false}
          - {columnName: COL_A, dataType: IntegerType, nullAble: false}
""")
    facts = parse_pipeline(f)
    assert len(facts.jobs[0].load.schema) == 2
    doc = build_design(facts)
    landing = next(s for s in doc.sheets if s.name.startswith("Landing - "))
    assert len(landing.rows) == 2


def test_weird_column_names_survive_rendering(tmp_path):
    """中文列名、带 # 的列名、超长列名都要能渲染。"""
    f = _write(tmp_path, """
kind: FactPipeline
metadata: {name: x}
spec:
  jobs:
    - jobType: eid
      extract: [{alias: A, connections: [{connector: cos, query: q}]}]
      load:
        metadata: {name: T}
        connections: [{connector: cos, query: out}]
        schema:
          - {columnName: "列 名", dataType: StringType, nullAble: true}
          - {columnName: "Invoice # or Voucher #", dataType: StringType, nullAble: true}
          - {columnName: "A_VERY_LONG_COLUMN_NAME_THAT_GOES_ON_AND_ON_AND_ON", dataType: StringType, nullAble: true}
""")
    facts = parse_pipeline(f)
    doc = build_design(facts)
    xlsx = render_xlsx(doc, tmp_path / "o.xlsx")
    assert xlsx.exists()
    assert validate_doc(doc, xlsx) == []


# ---------------------------------------------------------------- 规模
@pytest.mark.skipif(not CRD.exists(), reason="samples 缺失")
def test_sample_of_real_repo_never_crashes():
    """仓库里每个样例都要能解析 + 建文档，不能抛。

    这是"泛化性"的最低证据：不是只在我精心挑的那个文件上能跑。
    """
    files = sorted(CRD.rglob("*.yaml"))
    assert files, "samples/pipelines 为空"
    random.seed(20261118)

    ok = 0
    skipped = 0
    for f in files:
        try:
            facts = parse_pipeline(f)
        except PipelineParseError:
            skipped += 1
            continue
        build_design(facts)      # 关键：不能抛
        ok += 1

    assert ok == len(files), f"只成功 {ok}/{len(files)} 个，跳过 {skipped} 个"
