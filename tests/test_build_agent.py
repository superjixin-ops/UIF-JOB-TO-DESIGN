"""build 方向（文档 -> YAML）的测试。

测试组织按"裁判 -> 生成 -> 闭环"三层，和代码结构一一对应：

    1. 读文档不能丢信息（design_reader）
    2. 裁判要能抓住每一类错误（spec_validator / spec_grounding）
    3. 循环必须真的靠裁判收尾（yamlgen），而不是靠模型说"我完成了"

第 3 层全部用 ScriptedLLM，不联网。这样 CI 上跑的是"控制流正确"，
不是"模型当天心情好不好"——两件事必须分开测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdc.design import build_design
from pdc.design_reader import SpecReadError, read_design
from pdc.llm import LLMReply, ScriptedLLM, ToolCall
from pdc.parser import PipelineParseError, parse_pipeline
from pdc.render import render_xlsx
from pdc.spec_grounding import check, expected_items
from pdc.spec_validator import is_valid, validate_text
from pdc.yamlgen import requirement_from_text, run_build

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "pipelines"


# ---------------------------------------------------------------------------
# 1. 读文档：反向推导的上限由文档决定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sample", sorted(p.name for p in SAMPLES.glob("*.yaml")))
def test_roundtrip_reader_does_not_lose_information(sample: str, tmp_path: Path) -> None:
    """YAML -> xlsx -> spec，再拿 spec 去核对原始 facts，必须零违规。

    这条测试是整套 build 的地基：如果读文档就丢了列，
    模型再强也不可能生成对的东西，而失败会被误记成"模型能力不行"。
    """
    facts = parse_pipeline(SAMPLES / sample)
    doc = build_design(facts)
    xlsx = render_xlsx(doc, tmp_path / f"{doc.pipeline}.xlsx")
    spec = read_design(xlsx)

    violations = check(spec, facts)
    assert violations == [], f"{sample} 读回后丢信息: {[str(v) for v in violations]}"
    assert spec.pipeline == facts.name
    assert spec.jobs == tuple(j.job_type for j in facts.jobs)


def test_reader_rejects_foreign_workbook(tmp_path: Path) -> None:
    """结构不对要报错，不能猜。静默读错比读不出来危险。"""
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Sheet1"
    path = tmp_path / "foreign.xlsx"
    wb.save(path)

    with pytest.raises(SpecReadError, match="Overview"):
        read_design(path)


def test_transform_params_survive_the_document(tmp_path: Path) -> None:
    """重命名映射必须进文档。

    这是一次真实事故：Detail 列为了可读性丢掉了 dict/list 参数，
    反推时 mapColumns 整体消失，而当时的校验器只看 transformType，
    于是判了"通过"。YAML 能解析，跑起来列名全对不上。
    """
    facts = parse_pipeline(SAMPLES / "fact-order-fulfillment.yaml")
    xlsx = render_xlsx(build_design(facts), tmp_path / "rt.xlsx")
    spec = read_design(xlsx)

    rename = next(t for t in spec.transforms if t.transform_type == "RenameColumns")
    assert rename.params["mapColumns"]["ORDER_LINE_ID"] == "ORDER_LINE_NUMBER"

    select = next(t for t in spec.transforms if t.transform_type == "Select")
    assert "ORDER_ID" in select.params["columns"]


# ---------------------------------------------------------------------------
# 2. 裁判：每一类错误都要有专门的用例
# ---------------------------------------------------------------------------
BASE = """\
apiVersion: crd.example.io/v1
kind: DimensionPipeline
metadata:
  name: dim-tiny
spec:
  owner:
    name: Demo
  jobs:
    - jobType: eid
      setups:
        size: S
      extract:
        - extractType: default
          alias: TINY_SRC
          main_df: true
          connections:
            - credentials: demo-cos
              connector: cos
              query: landing/tiny.parquet
      transform:
        - transformType: Select
          objectNames:
            - TINY_SRC
          columns:
            - ID
          aliasResult: TINY_SRC
      load:
        metadata:
          name: DIM_TINY
        connections:
          - credentials: demo-cos
            connector: cos
            mode: overwrite
            loadObject: DIM_TINY
            partitionBy: []
            query: dim/DIM_TINY/transform.parquet
        schema:
          - columnName: ID
            dataType: StringType
            nullAble: false
"""


def codes(text: str) -> set[str]:
    return {i.code for i in validate_text(text)}


def test_base_yaml_is_valid() -> None:
    assert validate_text(BASE) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s.replace("kind: DimensionPipeline", "kind: Something"), "E_KIND"),
        (lambda s: s.replace("        size: S\n", ""), "E_SIZE"),
        (lambda s: s.replace("        size: S", "        size: HUGE"), "E_SIZE_VALUE"),
        (lambda s: s.replace("          main_df: true\n", ""), "W_MAIN_DF"),
        (
            lambda s: s.replace("            dataType: StringType\n", ""),
            "E_COL_TYPE",
        ),
        (
            lambda s: s.replace(
                "          - columnName: ID\n            dataType: StringType",
                "          - columnName: ID\n            dataType: StringType"
                "\n          - columnName: ID\n            dataType: StringType",
            ),
            "E_DUP_COL",
        ),
        (lambda s: s.replace("              query: landing", "              query: ../landing"), "W_PATH"),
    ],
)
def test_validator_catches_structural_errors(mutate, expected: str) -> None:
    broken = mutate(BASE)
    assert expected in codes(broken), f"应报 {expected}，实际 {codes(broken)}"


def test_two_main_df_is_legal() -> None:
    """一个 job 可以有两个主数据集。

    这条用例对应一次真实误判：校验器写了"只能有一个 main_df"，
    结果一条真实的库存流水线（快照 + 调整两个 fact 源 join）永远过不了。
    规则是从三个样例里总结出来的，**我把观察当成了规范**。
    """
    two = BASE.replace(
        "        - extractType: default\n          alias: TINY_SRC",
        "        - extractType: default\n          alias: EXTRA_SRC\n"
        "          main_df: true\n          connections:\n"
        "            - credentials: demo-cos\n              connector: cos\n"
        "              query: landing/extra.parquet\n"
        "        - extractType: default\n          alias: TINY_SRC",
        1,
    )
    assert "E_MAIN_DF" not in codes(two)


def test_validator_rejects_plaintext_secret() -> None:
    leaked = BASE.replace("spec:\n", "secret: abc123\nspec:\n")
    assert "E_SECRET" in codes(leaked)
    assert not is_valid(validate_text(leaked))


def test_validator_reports_semantic_structure_problem() -> None:
    """YAML 语法对、但 job 缺 jobType —— 不能崩，要转成可读错误。"""
    broken = BASE.replace("    - jobType: eid\n", "    - \n")
    issues = validate_text(broken)
    assert {i.code for i in issues} == {"E_PARSE_SEMANTIC"}
    assert "第 1 个 job" in issues[0].message


def test_grounding_catches_missing_and_fabricated(tmp_path: Path) -> None:
    facts = parse_pipeline(SAMPLES / "dim-product-family.yaml")
    xlsx = render_xlsx(build_design(facts), tmp_path / "p.xlsx")
    spec = read_design(xlsx)

    yaml_text = (SAMPLES / "dim-product-family.yaml").read_text(encoding="utf-8")
    good = tmp_path / "good.yaml"
    good.write_text(yaml_text, encoding="utf-8")
    assert check(spec, parse_pipeline(good)) == []

    # 漏一列：静默丢数据，必须报
    dropped = yaml_text.replace("- columnName: FAMILY_CODE\n", "- columnName: SOMETHING_ELSE\n", 1)
    dropped_file = tmp_path / "dropped.yaml"
    dropped_file.write_text(dropped, encoding="utf-8")
    assert "S_COL_MISSING" in {v.code for v in check(spec, parse_pipeline(dropped_file))}

    # 多一列：编造，必须报
    invented = yaml_text.replace(
        "- columnName: FAMILY_CODE\n",
        "- columnName: MADE_UP_COLUMN\n            dataType: StringType\n"
        "            nullAble: true\n          - columnName: FAMILY_CODE\n",
        1,
    )
    invented_file = tmp_path / "invented.yaml"
    invented_file.write_text(invented, encoding="utf-8")
    assert "S_COL_EXTRA" in {v.code for v in check(spec, parse_pipeline(invented_file))}


def test_grounding_catches_dropped_transform_params(tmp_path: Path) -> None:
    """这条用例对应真实事故：transformType 还在，参数没了。"""
    facts = parse_pipeline(SAMPLES / "fact-order-fulfillment.yaml")
    spec = read_design(render_xlsx(build_design(facts), tmp_path / "f.xlsx"))

    text = (SAMPLES / "fact-order-fulfillment.yaml").read_text(encoding="utf-8")
    # 删掉 RenameColumns 的映射
    start = text.index("          mapColumns:\n            ORDER_LINE_ID")
    end = text.index("          aliasResult: ORDER_LINES", start)
    broken = text[:start] + text[end:]
    path = tmp_path / "no_params.yaml"
    path.write_text(broken, encoding="utf-8")

    found = {v.code for v in check(spec, parse_pipeline(path))}
    assert "S_TRANSFORM_PARAMS" in found


def test_grounding_catches_wrong_mode(tmp_path: Path) -> None:
    facts = parse_pipeline(SAMPLES / "dim-product-family.yaml")
    spec = read_design(render_xlsx(build_design(facts), tmp_path / "m.xlsx"))
    text = (SAMPLES / "dim-product-family.yaml").read_text(encoding="utf-8")
    path = tmp_path / "mode.yaml"
    path.write_text(text.replace("mode: overwrite", "mode: append", 1), encoding="utf-8")
    assert "S_LOAD_MODE" in {v.code for v in check(spec, parse_pipeline(path))}


def test_error_message_uses_yaml_field_names(tmp_path: Path) -> None:
    """错误信息必须写给模型能直接照做的话。

    实测教训：原来写 "partition_by"，模型连续 8 轮不知道往哪个键放；
    改成 "在 load.connections[0] 下写 partitionBy" 之后 2 轮就过了。
    """
    facts = parse_pipeline(SAMPLES / "dim-product-family.yaml")
    spec = read_design(render_xlsx(build_design(facts), tmp_path / "e.xlsx"))
    text = (SAMPLES / "dim-product-family.yaml").read_text(encoding="utf-8")
    path = tmp_path / "nomode.yaml"
    path.write_text(text.replace("            mode: overwrite\n", "", 1), encoding="utf-8")

    messages = [v.message for v in check(spec, parse_pipeline(path))]
    assert any("load.connections[0].mode" in m for m in messages)


# ---------------------------------------------------------------------------
# 3. 生成循环：靠裁判收尾
# ---------------------------------------------------------------------------
def call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"c_{name}_{len(arguments)}", name=name, arguments=arguments)


def test_loop_passes_only_after_validator_agrees(tmp_path: Path) -> None:
    """先交一版错的，再交对的。终止条件是裁判通过，不是模型说完成。"""
    broken = BASE.replace("        size: S", "        size: HUGE")
    llm = ScriptedLLM(
        [
            LLMReply(content="先写一版", tool_calls=[call("write_yaml", text=broken)]),
            LLMReply(content="自检", tool_calls=[call("validate_yaml")]),
            LLMReply(content="修好了", tool_calls=[call("write_yaml", text=BASE)]),
            LLMReply(content="再自检", tool_calls=[call("validate_yaml")]),
            LLMReply(content="完成", tool_calls=[call("finish", summary="ok")]),
        ]
    )
    req = requirement_from_text("dim-tiny TINY_SRC cos DIM_TINY ID", "dim-tiny")
    result = run_build(llm, req, out_dir=tmp_path, examples_dir=tmp_path)

    assert result.passed
    assert result.rounds == 2
    assert Path(result.yaml_path).read_text(encoding="utf-8") == BASE


def test_finish_is_rejected_while_errors_remain(tmp_path: Path) -> None:
    """硬门禁：有错时 finish 必须被拒，并把错误清单退回去。"""
    broken = BASE.replace("        size: S", "        size: HUGE")
    llm = ScriptedLLM(
        [
            LLMReply(tool_calls=[call("write_yaml", text=broken)]),
            LLMReply(tool_calls=[call("finish", summary="做完了")]),
            # 模型收到驳回后不改，直接耗尽脚本
        ]
    )
    req = requirement_from_text("dim-tiny TINY_SRC cos DIM_TINY ID", "dim-tiny")
    result = run_build(llm, req, out_dir=tmp_path, examples_dir=tmp_path, max_rounds=1)

    assert not result.passed
    assert result.issues and result.issues[0]["code"] in {"E_SIZE_VALUE", "E_SIZE"}

    # 驳回信息确实回给了模型
    tool_payloads = [
        json.dumps(m, ensure_ascii=False)
        for m in llm.calls[-1]
        if m.get("role") == "tool"
    ]
    assert any("accepted" in p and "false" in p for p in tool_payloads)


def test_stuck_detection_fires_on_repeated_error_set(tmp_path: Path) -> None:
    """连续三轮同一组错误 -> 触发换策略提示，并记账。"""
    broken = BASE.replace("        size: S", "        size: HUGE")
    script = [
        LLMReply(tool_calls=[call("write_yaml", text=broken), call("validate_yaml")]),
        LLMReply(tool_calls=[call("validate_yaml")]),
        LLMReply(tool_calls=[call("validate_yaml")]),
        LLMReply(tool_calls=[call("validate_yaml")]),
    ]
    llm = ScriptedLLM(script)
    req = requirement_from_text("dim-tiny TINY_SRC cos DIM_TINY ID", "dim-tiny")
    result = run_build(llm, req, out_dir=tmp_path, examples_dir=tmp_path, max_rounds=3)

    assert result.stuck_events >= 1
    assert not result.passed


def test_stuck_detection_ignores_non_validation_steps(tmp_path: Path) -> None:
    """校验失败后去读示例，不算卡住。

    tools.issues 会一直保留到下一次 validate_yaml。如果每个 step 都判断，
    正常的"失败 -> 读示例 -> 重写"会被误判成连续三次同错，
    白注入一次 STUCK_PROMPT，也多花一次上下文。
    """
    broken = BASE.replace("        size: S", "        size: HUGE")
    script = [
        LLMReply(tool_calls=[call("write_yaml", text=broken), call("validate_yaml")]),
        LLMReply(tool_calls=[call("read_example", keyword="eid")]),
        LLMReply(tool_calls=[call("read_example", keyword="eid")]),
        LLMReply(tool_calls=[call("read_example", keyword="eid")]),
    ]
    llm = ScriptedLLM(script)
    req = requirement_from_text("dim-tiny TINY_SRC cos DIM_TINY ID", "dim-tiny")
    result = run_build(llm, req, out_dir=tmp_path, examples_dir=tmp_path, max_rounds=4)

    assert result.stuck_events == 0


def test_yaml_in_content_is_recovered(tmp_path: Path) -> None:
    """模型把 YAML 贴在正文里而不调工具时，兜底收下并记录。"""
    llm = ScriptedLLM(
        [
            LLMReply(content=f"这是配置：\n```yaml\n{BASE}\n```"),
            LLMReply(tool_calls=[call("validate_yaml")]),
            LLMReply(tool_calls=[call("finish", summary="ok")]),
        ]
    )
    req = requirement_from_text("dim-tiny TINY_SRC cos DIM_TINY ID", "dim-tiny")
    result = run_build(llm, req, out_dir=tmp_path, examples_dir=tmp_path)

    assert result.passed
    kinds = [r.kind for r in result.trace.steps]
    assert "safety" in kinds


def test_unparsed_output_never_reports_perfect_grounding(tmp_path: Path) -> None:
    """没解析成功时 grounding 必须是 0，不能报 1.0。

    这是上线前抓到的假通过：结构校验失败 -> 对齐检查没跑 -> 报告里 1.0。
    """
    llm = ScriptedLLM(
        [
            LLMReply(tool_calls=[call("write_yaml", text="kind: [unclosed")]),
            LLMReply(tool_calls=[call("validate_yaml")]),
        ]
    )
    req = requirement_from_text("dim-tiny TINY_SRC cos", "dim-tiny")
    result = run_build(llm, req, out_dir=tmp_path, examples_dir=tmp_path, max_rounds=1)

    assert not result.passed
    assert result.grounding == 0.0


def test_example_reads_are_capped(tmp_path: Path) -> None:
    """防止在"看示例"里打转：超过上限后工具从 schema 里消失。"""
    from pdc.yamlgen import BuildTools

    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "a.yaml").write_text(BASE, encoding="utf-8")
    req = requirement_from_text("dim-tiny TINY_SRC cos", "dim-tiny")
    tools = BuildTools(req, examples_dir=examples, out_dir=tmp_path)

    for _ in range(4):
        tools.call("read_example", {"name": "a.yaml"})
    names = [t["function"]["name"] for t in tools.schemas()]
    assert "read_example" not in names


def test_parser_turns_malformed_job_into_readable_error(tmp_path: Path) -> None:
    """模型产出的畸形 YAML 不能让解析器抛 KeyError。"""
    broken = BASE.replace("    - jobType: eid\n", "    -\n")
    path = tmp_path / "bad.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(PipelineParseError, match="第 1 个 job"):
        parse_pipeline(path)


def test_expected_items_counts_the_contract_not_the_output(tmp_path: Path) -> None:
    """分母必须按文档要求算，否则"漏得越多、分数越高"。"""
    facts = parse_pipeline(SAMPLES / "dim-product-family.yaml")
    spec = read_design(render_xlsx(build_design(facts), tmp_path / "i.xlsx"))
    assert expected_items(spec) >= sum(len(load.columns) for load in spec.loads)


# ---------------------------------------------------------------------------
# 4. 连接级参数：文档看不见的字段 = 校验器的盲区
# ---------------------------------------------------------------------------
def _spec_and_text(tmp_path: Path, name: str = "fact-order-fulfillment"):
    facts = parse_pipeline(SAMPLES / f"{name}.yaml")
    spec = read_design(render_xlsx(build_design(facts), tmp_path / "s.xlsx"))
    text = (SAMPLES / f"{name}.yaml").read_text(encoding="utf-8")
    return spec, text


def _codes_after(tmp_path: Path, text: str, name: str = "fact-order-fulfillment") -> set[str]:
    spec, _ = _spec_and_text(tmp_path, name)
    path = tmp_path / "mutated.yaml"
    path.write_text(text, encoding="utf-8")
    return {v.code for v in check(spec, parse_pipeline(path))}


def test_connection_params_are_contract(tmp_path: Path) -> None:
    """keyName / requestFlags / 连接别名必须比对。

    这些字段原来根本不进设计文档，于是模型丢掉它们也能判满分——
    实测一条 30 列的流水线丢了 14 个键值，报告写着 grounding=1.0。
    """
    _, text = _spec_and_text(tmp_path)

    no_key = text.replace("              keyName: SURROGATE_KEY\n", "", 1)
    assert "S_EXTRACT_PARAMS" in _codes_after(tmp_path, no_key)

    no_flag = text.replace("              requestFlags:\n                includeDeleted: false\n", "", 1)
    assert "S_EXTRACT_PARAMS" in _codes_after(tmp_path, no_flag)


def test_wrong_load_connector_is_caught(tmp_path: Path) -> None:
    """JDBC 目标写成 COS 会被判出来。

    这是真实发生过的错误：模型把 `jdbc`/`demo-db2` 写成 `cos`/`demo-cos`，
    而当时的校验器只核对"凭证名在允许集合里"，cos 恰好在集合里，于是放行。
    """
    _, text = _spec_and_text(tmp_path)
    broken = text.replace("            connector: jdbc\n", "            connector: cos\n", 1)
    assert "S_LOAD_PARAMS" in _codes_after(tmp_path, broken)


def test_missing_whole_load_is_caught(tmp_path: Path) -> None:
    """整段 load 消失不能算通过。

    原来的实现是 `if j.load is None: continue`，等于"没有写入就不检查"。
    模型删掉 JDBC 那段连接，报告仍然是 grounding=1.0。
    """
    _, text = _spec_and_text(tmp_path)
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if "jobType: datamart-self-serve" in line)
    load_at = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "load:")
    setups_at = next(i for i in range(load_at + 1, len(lines)) if lines[i].strip().startswith("setups:"))
    broken = "\n".join(lines[:load_at] + lines[setups_at:]) + "\n"
    assert "S_LOAD_MISSING" in _codes_after(tmp_path, broken)


def test_nullable_and_precision_are_contract(tmp_path: Path) -> None:
    """可空性和精度也是契约。

    模型把 `nullAble` 写成 `nullable`，解析器只认前者，字段被静默丢弃，
    而校验器当时只比列名和类型，判了通过。
    """
    _, text = _spec_and_text(tmp_path)

    bad_null = text.replace("            nullAble: true\n", "            nullAble: false\n", 1)
    assert "S_COL_NULLABLE" in _codes_after(tmp_path, bad_null)

    no_args = text.replace(
        "            dataTypeArgs:\n              precision: 18\n              scale: 2\n", "", 1
    )
    assert "S_COL_PRECISION" in _codes_after(tmp_path, no_args)


def test_job_size_is_contract(tmp_path: Path) -> None:
    """setups.size 写错不报错，但会改变 Spark 分片和成本。"""
    _, text = _spec_and_text(tmp_path)
    broken = text.replace("      setups:\n        size: L", "      setups:\n        size: XL", 1)
    assert "S_SIZE" in _codes_after(tmp_path, broken)


# ---------------------------------------------------------------------------
# 5. 逐键审计工具：测"你没想到的东西"
# ---------------------------------------------------------------------------
def test_fidelity_reports_zero_on_faithful_copy(tmp_path: Path) -> None:
    from eval.fidelity import audit

    src = SAMPLES / "fact-order-fulfillment.yaml"
    copy = tmp_path / "copy.yaml"
    copy.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    assert audit(src, copy) == []


def test_fidelity_catches_connection_level_drift(tmp_path: Path) -> None:
    """连接级字段掉了必须能报出来——这是校验器报 1.0 的那次事故。"""
    from eval.fidelity import audit

    src = SAMPLES / "fact-order-fulfillment.yaml"
    broken = tmp_path / "broken.yaml"
    text = src.read_text(encoding="utf-8").replace("              keyName: SURROGATE_KEY\n", "", 1)
    broken.write_text(text, encoding="utf-8")

    diffs = audit(src, broken)
    assert diffs
    assert any("keyName" in str(d) for d in diffs)


def test_fidelity_catches_fabricated_field(tmp_path: Path) -> None:
    """多出来的字段也要报。编造和遗漏一样是失真。"""
    from eval.fidelity import audit

    src = SAMPLES / "dim-product-family.yaml"
    broken = tmp_path / "extra.yaml"
    text = src.read_text(encoding="utf-8").replace(
        "            connector: cos\n", "            connector: cos\n            madeUpKey: x\n", 1
    )
    broken.write_text(text, encoding="utf-8")

    diffs = audit(src, broken)
    assert any("madeUpKey" in str(d) for d in diffs)


# ---------------------------------------------------------------------------
# 6. 真实语料逼出来的形状（CRD-7 体检）
# ---------------------------------------------------------------------------
def test_schema_template_does_not_crash() -> None:
    """`schema: Template` 是字符串，不是列表。

    真实语料里有大量这种写法（引用模板，不是内联列）。
    第一版校验器直接对它 .get()，整个进程崩掉。
    """
    text = BASE.replace("        schema:\n", "        schema: Template\n        schema2:\n")
    issues = validate_text(text)
    assert any(i.code == "W_SCHEMA_TEMPLATE" for i in issues)
    # 只是警告，不能阻断——模板引用是合法写法
    assert not any(i.severity == "error" for i in issues)


def test_template_ref_extract_is_not_an_error() -> None:
    """模板引用型 extract 没有 connections，是合法写法。"""
    text = BASE.replace(
        """        - extractType: default
          alias: TINY_SRC
          main_df: true
          connections:
            - credentials: demo-cos
              connector: cos
              query: landing/tiny.parquet""",
        """        - templateName: tiny-extract-template""",
    )
    codes = {i.code for i in validate_text(text)}
    assert "W_TEMPLATE_REF" in codes
    assert "E_CONN" not in codes


def test_missing_connector_is_a_warning_not_a_blocker() -> None:
    """connection 省略 connector 在真实语料里很常见（credentials 已隐含）。

    强制要求会把大量合法文件判死，也会逼模型编一个连接器出来。
    """
    text = BASE.replace("              connector: cos\n", "", 1)
    issues = validate_text(text)
    assert "W_CONN" in {i.code for i in issues}
    assert not any(i.severity == "error" for i in issues)


def test_secret_regex_only_flags_realistic_keys() -> None:
    """占位标识符不能报成密钥。

    真实语料里出现过 25 字符的 `sk-...` 标识符，被误报成泄漏。
    真实 key 至少 32 位（DeepSeek 32、OpenAI 48）。
    """
    short = BASE.replace("name: dim-tiny", "name: dim-tiny\n  note: sk-abc-def-ghi-jkl")
    assert "E_SECRET" not in {i.code for i in validate_text(short)}

    long_key = "sk-" + "a" * 40
    leaked = BASE.replace("name: dim-tiny", f"name: dim-tiny\n  note: {long_key}")
    assert "E_SECRET" in {i.code for i in validate_text(leaked)}


# ---------------------------------------------------------------------------
# 7. 示例截断必须保住 load 段
# ---------------------------------------------------------------------------
def test_excerpt_keeps_load_block_when_truncating() -> None:
    """长文件的 load 段不能被截断切掉。

    真实事故：600 行的示例，load 在第 400 行。只给前 220 行，
    模型永远看不到 schema 长什么样，生成时整段省略 schema——
    一次 12×3 的 A/B 因此被污染。
    """
    from pdc.yamlgen import _excerpt

    lines = ["# head"] * 400 + [
        "      load:",
        "        metadata:",
        "          name: DIM_X",
        "        schema:",
        "          - columnName: ID",
        "            dataType: StringType",
        "      setups:",
        "        size: S",
    ]
    content, mode = _excerpt(lines, 220)
    assert mode == "head+load"
    assert "columnName: ID" in content
    assert "dataType: StringType" in content
    assert "setups:" not in content  # 只保留 load 段，不越界
    assert len(content.splitlines()) <= 221


def test_excerpt_returns_full_when_short() -> None:
    from pdc.yamlgen import _excerpt

    lines = ["a: 1", "b: 2"]
    content, mode = _excerpt(lines, 220)
    assert mode == "full"
    assert content == "a: 1\nb: 2"


def test_excerpt_without_load_falls_back_to_head() -> None:
    from pdc.yamlgen import _excerpt

    content, mode = _excerpt([f"line{i}" for i in range(300)], 220)
    assert mode == "head"
    assert len(content.splitlines()) <= 220
