"""变换字典的测试。

这里测的不是"能不能解析"，是三件事：

    1. 文档解析不会把说明里的 <code> 当成参数取值
    2. 信任规则在六种情况下各给出正确的 trust
    3. 拿字典检查 pipeline 时，缺必填 / 多余参数 / 未知类型都能报出来
"""

from __future__ import annotations

from pdc.catalog import (
    CONFLICT,
    CONVENTIONAL,
    DEPRECATED,
    OPTIONAL,
    REQUIRED,
    UNDOCUMENTED,
    build,
    check,
    fill_defaults,
    parse_doc_html,
    parse_source,
    preferred,
    scan_corpus,
    scan_sources,
    trust_of,
)
from pdc.parser import parse_pipeline
from pdc.profiles import join_step


class TestParseDoc:
    def test_required_and_default(self) -> None:
        html = """
        <h2 id="sort">Sort</h2>
        <p><strong>Parameters:</strong></p>
        <ul>
        <li><p><strong>orderBy</strong> (list, defaults to None) - 排序字段</p></li>
        <li><p><strong>ascendingOrder</strong> (bool, defaults to False) - 默认降序</p></li>
        </ul>
        <h2 id="next">Next</h2>
        """
        got = parse_doc_html(html, "core")
        assert set(got) == {"Sort"}
        params = {p["name"]: p for p in got["Sort"]["params"]}
        assert params["orderBy"]["required"] is False
        assert params["orderBy"]["default"] == "None"
        assert params["ascendingOrder"]["default"] == "False"

    def test_star_marks_required(self) -> None:
        html = """
        <h2 id="cos">COSConnector</h2>
        <ul>
        <li><p><strong>credentials*</strong> (str) - 凭证名</p></li>
        <li><p><strong>connector*</strong> (str) - <code>cos</code></p></li>
        <li><p><strong>countRows*</strong> (bool, defaults to False) - 计数</p></li>
        </ul>
        """
        got = parse_doc_html(html, "conn")
        params = {p["name"]: p for p in got["COSConnector"]["params"]}
        assert params["credentials"]["required"] is True
        assert params["countRows"]["required"] is True
        assert params["countRows"]["default"] == "False"

    def test_code_in_prose_is_not_a_value(self) -> None:
        # 第一次按位置截取，把上一段的 <code> 抓成了参数取值。
        html = """
        <h2 id="x">X</h2>
        <p>参见 <code>&gt;</code> 和 <code>W-MON</code>。</p>
        <ul>
        <li><p><strong>connector*</strong> (str) - <code>cos</code></p></li>
        </ul>
        """
        got = parse_doc_html(html, "p")
        params = {p["name"]: p for p in got["X"]["params"]}
        assert params["connector"]["codes"] == ["cos"]


class TestTrust:
    def test_schema_required_wins(self) -> None:
        assert trust_of(
            doc_present=False, doc_required=False, schema_required=True, rate=0, count=100
        ) == REQUIRED

    def test_doc_required_and_common(self) -> None:
        assert trust_of(
            doc_present=True, doc_required=True, schema_required=False, rate=0.9, count=100
        ) == REQUIRED

    def test_doc_required_but_rare_is_conflict(self) -> None:
        # Join.joinMapping：文档说必填，语料 0%
        assert trust_of(
            doc_present=True, doc_required=True, schema_required=False, rate=0.0, count=100
        ) == CONFLICT

    def test_optional_but_always_is_conventional(self) -> None:
        # Sort.orderBy：文档说可选，语料 100%
        assert trust_of(
            doc_present=True, doc_required=False, schema_required=False, rate=1.0, count=100
        ) == CONVENTIONAL

    def test_undocumented_but_always(self) -> None:
        # box.plugin：文档没写，语料 100%
        assert trust_of(
            doc_present=False, doc_required=False, schema_required=False, rate=1.0, count=100
        ) == UNDOCUMENTED

    def test_documented_but_never_used_is_deprecated(self) -> None:
        assert trust_of(
            doc_present=True, doc_required=False, schema_required=False, rate=0.0, count=100
        ) == DEPRECATED

    def test_rare_param_stays_optional(self) -> None:
        assert trust_of(
            doc_present=True, doc_required=False, schema_required=False, rate=0.1, count=100
        ) == OPTIONAL

    def test_small_sample_is_not_a_convention(self) -> None:
        # 3 条流水线都写，不等于惯例。
        assert trust_of(
            doc_present=True, doc_required=False, schema_required=False, rate=1.0, count=3
        ) == OPTIONAL


def _corpus() -> dict:
    return {
        "files": 20,
        "transforms": {
            "Sort": {
                "count": 20,
                "keys": {"orderBy": 20, "ascendingOrder": 10},
                "samples": [{"orderBy": ["X"]}],
            }
        },
        "extracts": {"cos": {"count": 20, "keys": {"query": 20}, "samples": []}},
        "loads": {},
    }


def _docs() -> dict:
    return {
        "Sort": {
            "page": "core",
            "params": [
                {"name": "orderBy", "required": False, "default": None},
                {"name": "ascendingOrder", "required": False, "default": "False"},
            ],
        }
    }


def _schema() -> dict:
    return {
        "transforms": {"Sort": {"required": ["orderBy"], "props": ["orderBy", "ascendingOrder"]}},
        "connectors": {"cos": {"required": ["query"], "props": ["query"]}},
        "connector_enum": ["cos"],
    }


class TestBuild:
    def test_cards(self) -> None:
        built = build(_corpus(), _docs(), _schema())
        card = built["transforms"]["Sort"]
        assert card["count"] == 20
        assert card["params"]["orderBy"]["trust"] == REQUIRED
        assert card["params"]["ascendingOrder"]["trust"] == OPTIONAL
        assert card["samples"] == [{"orderBy": ["X"]}]

    def test_flags_only_for_problems(self) -> None:
        built = build(_corpus(), _docs(), _schema())
        assert built["transforms"]["Sort"]["flags"] == []

    def test_connector_docs_are_keyed_by_value(self) -> None:
        """文档标题是 COSConnector，语料里是 cos。不映射就全成"文档没写"。"""
        docs = {
            "COSConnector": {
                "page": "conn",
                "params": [
                    {"name": "connector", "required": True, "codes": ["cos"]},
                    {"name": "query", "required": True, "codes": []},
                ],
            }
        }
        built = build(_corpus(), docs, {**_schema(), "connectors": {"cos": {"required": [], "props": ["query"]}}})
        assert built["connectors"]["cos"]["params"]["query"]["trust"] == REQUIRED


class TestCheck:
    def _pipeline(self, tmp_path, transform: str) -> str:
        path = tmp_path / "p.yaml"
        path.write_text(
            "apiVersion: crd.ibm.k8s.io/v1\n"
            "kind: FactPipeline\n"
            "metadata: {name: t}\n"
            "spec:\n"
            "  jobs:\n"
            "    - jobType: eid\n"
            "      setups: {size: T}\n"
            "      extract:\n"
            "        - alias: A\n"
            "          main_df: true\n"
            "          connections:\n"
            "            - connector: cos\n"
            "              credentials: cos\n"
            "              query: a/b\n"
            "      transform:\n"
            + transform
            + "      load:\n"
            "        connections:\n"
            "          - connector: cos\n"
            "            credentials: cos\n"
            "            query: a/b\n",
            encoding="utf-8",
        )
        return str(path)

    def test_missing_required(self, tmp_path) -> None:
        path = self._pipeline(
            tmp_path,
            "        - transformType: Sort\n"
            "          objectNames: [A]\n"
            "          aliasResult: A\n",
        )
        built = build(_corpus(), _docs(), _schema())
        codes = [i.code for i in check(parse_pipeline(path), built)]
        assert "C_MISSING_PARAM" in codes

    def test_unknown_param(self, tmp_path) -> None:
        path = self._pipeline(
            tmp_path,
            "        - transformType: Sort\n"
            "          objectNames: [A]\n"
            "          orderBy: [X]\n"
            "          typoParam: 1\n"
            "          aliasResult: A\n",
        )
        built = build(_corpus(), _docs(), _schema())
        codes = [i.code for i in check(parse_pipeline(path), built)]
        assert "C_UNKNOWN_PARAM" in codes

    def test_unknown_type(self, tmp_path) -> None:
        path = self._pipeline(
            tmp_path,
            "        - transformType: NotATransform\n"
            "          objectNames: [A]\n"
            "          aliasResult: A\n",
        )
        built = build(_corpus(), _docs(), _schema())
        codes = [i.code for i in check(parse_pipeline(path), built)]
        assert "C_UNKNOWN_TYPE" in codes

    def test_clean_pipeline(self, tmp_path) -> None:
        path = self._pipeline(
            tmp_path,
            "        - transformType: Sort\n"
            "          objectNames: [A]\n"
            "          orderBy: [X]\n"
            "          aliasResult: A\n",
        )
        built = build(_corpus(), _docs(), _schema())
        assert check(parse_pipeline(path), built) == []


class TestScanCorpus:
    def test_counts_and_samples(self, tmp_path) -> None:
        (tmp_path / "p.yaml").write_text(
            "apiVersion: crd.ibm.k8s.io/v1\n"
            "kind: FactPipeline\n"
            "metadata: {name: t}\n"
            "spec:\n"
            "  jobs:\n"
            "    - jobType: eid\n"
            "      setups: {size: T}\n"
            "      extract:\n"
            "        - alias: A\n"
            "          connections:\n"
            "            - connector: cos\n"
            "              credentials: cos\n"
            "              query: a/b\n"
            "      transform:\n"
            "        - transformType: Sort\n"
            "          objectNames: [A]\n"
            "          orderBy: [X]\n"
            "          aliasResult: A\n",
            encoding="utf-8",
        )
        got = scan_corpus(tmp_path)
        assert got["files"] == 1
        assert got["transforms"]["Sort"]["count"] == 1
        assert got["transforms"]["Sort"]["keys"]["orderBy"] == 1
        assert got["extracts"]["cos"]["keys"]["query"] == 1
        # 样例里不带通用字段
        assert "transformType" not in got["transforms"]["Sort"]["samples"][0]


    def test_exclude_paths(self, tmp_path) -> None:
        """留一法评测：把目标 pipeline 从语料里剔除再建字典。"""
        f = tmp_path / "a.yaml"
        f.write_text(
            "apiVersion: crd.ibm.k8s.io/v1\n"
            "kind: FactPipeline\n"
            "metadata: {name: t}\n"
            "spec:\n"
            "  jobs:\n"
            "    - jobType: eid\n"
            "      setups: {size: T}\n"
            "      extract: []\n",
            encoding="utf-8",
        )
        assert scan_corpus(tmp_path)["files"] == 1
        assert scan_corpus(tmp_path, exclude={str(f)})["files"] == 0


class TestJoinDialect:
    def test_legacy_uses_map_columns(self) -> None:
        step = join_step("A", "B", {"ID": "ID"}, alias="A")
        assert step["mapColumns"] == {"ID": "ID"}
        assert "joinMapping" not in step
        assert step["how"] == "left"

    def test_current_uses_join_mapping(self) -> None:
        step = join_step("A", "B", {"ID": "ID"}, alias="A", dialect="current")
        assert step["joinMapping"] == {"A.ID": "B.ID"}
        assert step["left"] == "A" and step["right"] == "B"
        assert "mapColumns" not in step


SOURCE = '''
class AddSurrogateKey(Transformation):
    def __init__(
        self,
        spark_factory: SparkFactory,
        config: CredentialsHelper,
        naturalKeys: list,
        surrogateKeyName: str,
        dimensionType: Optional[str] = "two",
        deltaColumns: Optional[list] = None,
        objectNames: Optional[list] = None,
    ) -> None:
        pass
'''


class TestSource:
    def test_required_vs_optional(self) -> None:
        got = parse_source(SOURCE, "add_surrogate_key")
        params = got["AddSurrogateKey"]["params"]
        assert params["naturalKeys"]["required"] is True
        assert params["surrogateKeyName"]["required"] is True
        assert params["dimensionType"]["required"] is False
        assert params["dimensionType"]["default"] == '"two"'
        assert params["deltaColumns"]["default"] == "None"

    def test_untyped_params_are_kept(self) -> None:
        # 真实源码：def __init__(self, spark_factory, columns, value, objectNames=None)
        src = """
class AddColumns(Transformation):
    def __init__(self, spark_factory, columns, value, objectNames=None):
        pass
"""
        params = parse_source(src)["AddColumns"]["params"]
        assert params["columns"]["required"] is True
        assert params["value"]["required"] is True
        assert "objectNames" not in params

    def test_injected_params_are_skipped(self) -> None:
        params = parse_source(SOURCE)["AddSurrogateKey"]["params"]
        for name in ("self", "spark_factory", "config", "objectNames"):
            assert name not in params

    def test_scan_sources_recurses(self, tmp_path) -> None:
        # transformations/ 下面还有 core / dimensions / datamart 子目录。
        sub = tmp_path / "core"
        sub.mkdir()
        (sub / "sort.py").write_text(
            "class Sort(Transformation):\n"
            "    def __init__(self, spark_factory, orderBy=None):\n"
            "        pass\n",
            encoding="utf-8",
        )
        got = scan_sources(tmp_path)
        assert "Sort" in got
        assert got["Sort"]["params"]["orderBy"]["required"] is False

    def test_source_required_beats_missing_docs(self) -> None:
        corpus = {
            "files": 100,
            "transforms": {
                "AddSurrogateKey": {
                    "count": 100,
                    "keys": {"naturalKeys": 100, "dimensionType": 5},
                    "samples": [],
                }
            },
            "extracts": {},
            "loads": {},
        }
        sources = parse_source(SOURCE)
        built = build(
            corpus,
            {},
            {"transforms": {}, "connectors": {}, "connector_enum": []},
            sources,
        )
        card = built["transforms"]["AddSurrogateKey"]
        assert card["params"]["naturalKeys"]["trust"] == REQUIRED
        # 源码给了默认值，语料 5% 才写 → 可选，不是"废弃"
        assert card["params"]["dimensionType"]["trust"] == OPTIONAL


class TestJoinCheck:
    def _catalog(self) -> dict:
        corpus = {
            "files": 100,
            "transforms": {
                "Join": {
                    "count": 100,
                    "keys": {"mapColumns": 100, "how": 100},
                    "samples": [],
                }
            },
            "extracts": {},
            "loads": {},
        }
        return build(
            corpus,
            {},
            {"transforms": {"Join": {"required": [], "props": []}}, "connectors": {}, "connector_enum": []},
        )

    def _pipeline(self, tmp_path, params: str) -> str:
        path = tmp_path / "join.yaml"
        path.write_text(
            "apiVersion: crd.ibm.k8s.io/v1\n"
            "kind: FactPipeline\n"
            "metadata: {name: t}\n"
            "spec:\n"
            "  jobs:\n"
            "    - jobType: eid\n"
            "      setups: {size: T}\n"
            "      extract:\n"
            "        - alias: A\n"
            "          connections:\n"
            "            - connector: cos\n"
            "              credentials: cos\n"
            "              query: a/b\n"
            "      transform:\n"
            "        - transformType: Join\n"
            "          objectNames: [A, B]\n"
            + params
            + "          aliasResult: A\n",
            encoding="utf-8",
        )
        return str(path)

    def test_mixed_dialects(self, tmp_path) -> None:
        path = self._pipeline(
            tmp_path,
            "          mapColumns: {ID: ID}\n"
            "          joinMapping: {A.ID: B.ID}\n"
            "          left: A\n"
            "          right: B\n",
        )
        codes = [i.code for i in check(parse_pipeline(path), self._catalog())]
        assert "C_JOIN_DIALECT_MIXED" in codes

    def _catalog_with_conflict(self) -> dict:
        corpus = {
            "files": 100,
            "transforms": {
                "Join": {"count": 100, "keys": {"mapColumns": 100}, "samples": []}
            },
            "extracts": {},
            "loads": {},
        }
        docs = {
            "Join": {
                "page": "core",
                "params": [{"name": "joinMapping", "required": True, "default": None}],
            }
        }
        return build(
            corpus,
            docs,
            {"transforms": {"Join": {"required": [], "props": []}}, "connectors": {}, "connector_enum": []},
        )

    def test_legacy_join_does_not_report_current_conflict(self, tmp_path) -> None:
        """选了 legacy 方言，就不该再因为没写 current 参数报警。"""
        path = self._pipeline(tmp_path, "          mapColumns: {ID: ID}\n")
        codes = [i.code for i in check(parse_pipeline(path), self._catalog_with_conflict())]
        assert "C_CONFLICT_PARAM" not in codes

    def test_join_without_any_dialect_is_reported(self, tmp_path) -> None:
        path = self._pipeline(tmp_path, "          how: left\n")
        codes = [i.code for i in check(parse_pipeline(path), self._catalog_with_conflict())]
        assert "C_CONFLICT_PARAM" in codes

    def test_current_without_left_right(self, tmp_path) -> None:
        path = self._pipeline(
            tmp_path,
            "          joinMapping: {A.ID: B.ID}\n",
        )
        codes = [i.code for i in check(parse_pipeline(path), self._catalog())]
        assert "C_JOIN_DIALECT_INCOMPLETE" in codes


class TestFillDefaults:
    def _plan(self, ttype: str = "Union") -> dict:
        return {
            "spec": {
                "jobs": [
                    {
                        "jobType": "eid",
                        "transform": [{"transformType": ttype, "objectNames": ["A"]}],
                    }
                ]
            }
        }

    def _catalog(self, params: dict) -> dict:
        return {"transforms": {"Union": {"params": params}}}

    def test_fills_source_confirmed_default(self) -> None:
        catalog = self._catalog({
            "distinct": {
                "trust": OPTIONAL, "rate": 0.31, "default": "False",
                "source_present": True, "source_required": False,
            }
        })
        out, filled = fill_defaults(self._plan(), catalog)
        assert out["spec"]["jobs"][0]["transform"][0]["distinct"] is False
        assert filled == ["Union.distinct=False"]

    def test_skips_params_nobody_writes(self) -> None:
        catalog = self._catalog({
            "useContains": {
                "trust": OPTIONAL, "rate": 0.0, "default": "False",
                "source_present": True, "source_required": False,
            }
        })
        out, filled = fill_defaults(self._plan(), catalog)
        assert "useContains" not in out["spec"]["jobs"][0]["transform"][0]
        assert filled == []

    def test_skips_docs_only_param_when_source_exists(self) -> None:
        # UniqueID.sequential：文档有、源码没有，写进去就是非法 kwarg。
        catalog = self._catalog({
            "mode": {
                "trust": CONVENTIONAL, "rate": 0.98, "default": "overwrite",
                "source_present": True, "source_required": False,
            },
            "sequential": {
                "trust": OPTIONAL, "rate": 0.5, "default": "False",
                "source_present": False, "source_required": False,
            },
        })
        out, _ = fill_defaults(self._plan(), catalog)
        t = out["spec"]["jobs"][0]["transform"][0]
        assert t["mode"] == "overwrite"
        assert "sequential" not in t

    def test_uses_docs_when_type_has_no_source(self) -> None:
        # Sort 在 prelude 包里，没有源码，只能信文档。
        catalog = {
            "transforms": {
                "Union": {
                    "params": {
                        "ascendingOrder": {
                            "trust": OPTIONAL, "rate": 0.47, "default": "False",
                            "source_present": False, "source_required": False,
                        }
                    }
                }
            }
        }
        out, filled = fill_defaults(self._plan(), catalog)
        assert out["spec"]["jobs"][0]["transform"][0]["ascendingOrder"] is False
        assert filled == ["Union.ascendingOrder=False"]

    def test_does_not_mutate_input(self) -> None:
        catalog = self._catalog({
            "distinct": {
                "trust": OPTIONAL, "rate": 0.31, "default": "False",
                "source_present": True, "source_required": False,
            }
        })
        plan = self._plan()
        fill_defaults(plan, catalog)
        assert "distinct" not in plan["spec"]["jobs"][0]["transform"][0]


class TestPreferred:
    def test_picks_the_more_common(self) -> None:
        catalog = {"transforms": {"SelectDistinct": {"count": 3101}, "DropDupes": {"count": 1586}}}
        assert preferred(["DropDupes", "SelectDistinct"], catalog) == "SelectDistinct"

    def test_unknown_types_are_ignored(self) -> None:
        catalog = {"transforms": {"SelectDistinct": {"count": 10}}}
        assert preferred(["DropDupes", "SelectDistinct"], catalog) == "SelectDistinct"

    def test_no_catalog_returns_none(self) -> None:
        assert preferred(["SelectDistinct", "DropDupes"], None) is None
