"""LangChain 引擎与 pgvector 后端的测试。

两者都是"可选组件"：
  - LangChain：验证工具转换正确、缺 key 时报错清晰（不联网）
  - pgvector：验证向量字面量和表名校验；真实往返需要 Postgres，按环境变量跳过
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from pdc.langchain_engine import build_tools
from pdc.tools import Toolbox
from pdc.vectorstore import PgVectorStore, _vec_literal


def _toolbox(tmp_path: Path) -> Toolbox:
    return Toolbox(
        repo=Path("samples/pipelines"),
        out_dir=tmp_path,
        rules_dir=Path("samples/rules"),
    )


def test_langchain_tools_mirror_toolbox(tmp_path) -> None:
    """工具的名字和参数必须和 Toolbox 完全一致。

    转换层一旦漂移，两个引擎的对比就没有意义——
    你比较的不是"框架 vs 手写"，而是"两套不同的工具"。
    """
    tb = _toolbox(tmp_path)
    tools = {t.name: t for t in build_tools(tb)}
    expected = {s["function"]["name"] for s in tb.tool_schemas()}
    assert set(tools) == expected

    # 最简单的工具能真的调用，并返回统一结构
    out = tools["list_pipelines"].invoke({"limit": 2})
    assert '"ok"' in out


def test_langchain_requires_key(monkeypatch, tmp_path) -> None:
    from pdc.langchain_engine import run_langchain

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="key"):
        run_langchain(_toolbox(tmp_path), "test")


def test_pg_vector_literal() -> None:
    assert _vec_literal(np.array([1.0, 2.5], dtype=np.float32)) == "[1.000000,2.500000]"


def test_pg_rejects_bad_table_name() -> None:
    """表名会拼进 SQL，必须白名单校验。"""
    with pytest.raises(ValueError, match="表名"):
        PgVectorStore("postgresql://x", 3, table="x; drop table users")


@pytest.mark.skipif(
    not os.getenv("PDC_TEST_PG_DSN"), reason="需要 Postgres+pgvector（docker-compose.pg.yml）"
)
def test_pg_roundtrip() -> None:
    dsn = os.environ["PDC_TEST_PG_DSN"]
    store = PgVectorStore(dsn, dim=4, table="pdc_test_vectors")
    store.reset()
    store.add(
        [
            ("a", np.array([1, 0, 0, 0], dtype=np.float32), {"chain": "eid"}),
            ("b", np.array([0, 1, 0, 0], dtype=np.float32), {"chain": "dm"}),
        ]
    )
    hits = store.search(np.array([0.9, 0.1, 0, 0], dtype=np.float32), k=1)
    assert hits[0].id == "a"
    assert hits[0].score > 0.9

    filtered = store.search(np.array([0.9, 0.1, 0, 0], dtype=np.float32), k=2, where={"chain": "dm"})
    assert [h.id for h in filtered] == ["b"]
    store.reset()
    store.close()
