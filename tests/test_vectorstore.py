"""向量索引的测试：后端一致性、过滤、查询扩展。

测的是"两个后端会不会给出不同答案"，不是"检索准不准"。
准不准要靠标注集（目前只有 14 条样例语料的 query）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pdc.search import expand_query
from pdc.vectorstore import NumpyStore, SqliteVecStore


def _items(n: int = 50, dim: int = 8, seed: int = 0):
    rng = np.random.default_rng(seed)
    vectors = rng.normal(size=(n, dim)).astype(np.float32)
    ids = [f"p{i}" for i in range(n)]
    metas = [{"chain": "eid" if i % 2 == 0 else "dm", "path": f"p{i}.yaml"} for i in range(n)]
    return list(zip(ids, vectors, metas, strict=True))


def test_backends_return_same_top1(tmp_path) -> None:
    """同一份向量，numpy 和 sqlite-vec 的 top-1 必须一致。

    如果这里不一致，说明距离度量或归一化有问题——
    后面的效果对比就全部不可信。
    """
    items = _items()
    np_store = NumpyStore()
    np_store.add(items)

    sv_store = SqliteVecStore(tmp_path / "v.db", dim=8)
    sv_store.add(items)

    rng = np.random.default_rng(1)
    for _ in range(10):
        q = rng.normal(size=8).astype(np.float32)
        assert np_store.search(q, k=1)[0].id == sv_store.search(q, k=1)[0].id
    sv_store.close()


def test_where_filter_works_on_both(tmp_path) -> None:
    items = _items()
    np_store = NumpyStore()
    np_store.add(items)
    sv_store = SqliteVecStore(tmp_path / "v.db", dim=8)
    sv_store.add(items)

    q = np.ones(8, dtype=np.float32)
    for store in (np_store, sv_store):
        hits = store.search(q, k=5, where={"chain": "dm"})
        assert hits, "过滤后应该有结果"
        assert all(h.metadata["chain"] == "dm" for h in hits)
    sv_store.close()


def test_count_matches(tmp_path) -> None:
    items = _items(7)
    np_store = NumpyStore()
    np_store.add(items)
    sv_store = SqliteVecStore(tmp_path / "v.db", dim=8)
    sv_store.add(items)
    assert np_store.count() == sv_store.count() == 7
    sv_store.close()


def test_upsert_does_not_duplicate(tmp_path) -> None:
    """同一个 id 写两次，不能变成两条。"""
    sv_store = SqliteVecStore(tmp_path / "v.db", dim=8)
    item = ("p0", np.ones(8, dtype=np.float32), {"chain": "eid"})
    sv_store.add([item])
    sv_store.add([item])
    assert sv_store.count() == 1
    sv_store.close()


def test_expand_query_bridges_chinese_to_english() -> None:
    expanded = expand_query("订单履约的流水线")
    assert "order" in expanded
    assert "fulfillment" in expanded
    # 纯英文查询不应被改动
    assert expand_query("order fulfillment") == "order fulfillment"


def test_card_text_does_not_leak_schema_keywords() -> None:
    """卡片是业务投影，不能把 YAML 的结构键也塞进去。

    否则 embedding 会被 jobType / columnName 这类词淹没，
    又回到"按格式聚类"的老问题。
    """
    from pdc.corpus_index import build_cards

    cards = build_cards("samples/pipelines")
    assert cards
    for card in cards:
        for key in ("jobType", "columnName", "dataType", "nullAble", "connections"):
            assert key not in card.text


def test_index_and_search_roundtrip(tmp_path) -> None:
    """离线向量下，用卡片原文查询必须命中自己。"""
    from pdc.search import build, search

    build("samples/pipelines", tmp_path, backend="numpy", offline_embed=True)
    from pdc.corpus_index import read_cards

    target = read_cards(tmp_path / "cards.jsonl")[0]
    hits = search(tmp_path, target["text"], k=1, offline_embed=True)
    assert hits[0].id == target["id"]


def test_sqlite_backend_roundtrip(tmp_path) -> None:
    from pdc.search import build, search

    meta = build("samples/pipelines", tmp_path, backend="sqlite-vec", offline_embed=True)
    assert meta["backend"] == "sqlite-vec"
    hits = search(tmp_path, "dim-product-family", k=2, offline_embed=True)
    assert hits


# ---------------------------------------------------------------------------
# 增量索引：只 embed 变化的部分
# ---------------------------------------------------------------------------
def _copy_samples(tmp_path: Path) -> Path:
    import shutil

    src = Path("samples/pipelines")
    dst = tmp_path / "repo"
    dst.mkdir()
    for p in src.glob("*.yaml"):
        shutil.copyfile(p, dst / p.name)
    return dst


def test_incremental_update_embeds_only_changes(tmp_path) -> None:
    """全量重建是分钟级，增量应该是秒级——这是向量库的卖点，必须真的做出来。"""
    from pdc.search import build, update

    repo = _copy_samples(tmp_path)
    out = tmp_path / "index"
    build(repo, out, backend="numpy", offline_embed=True)

    noop = update(repo, out, offline_embed=True)
    assert noop["embedded"] == 0 and noop["count"] == 4

    target = repo / "fact-order-fulfillment.yaml"
    target.write_text(target.read_text(encoding="utf-8").replace("jobType: eid", "jobType: eid-v2", 1))
    changed = update(repo, out, offline_embed=True)
    assert changed["changed"] == 1
    assert changed["embedded"] == 1
    assert changed["count"] == 4

    (repo / "dim-product-family.yaml").unlink()
    removed = update(repo, out, offline_embed=True)
    assert removed["removed"] == 1
    assert removed["count"] == 3
    assert removed["embedded"] == 0


def test_incremental_update_sqlite_backend(tmp_path) -> None:
    from pdc.search import build, update

    repo = _copy_samples(tmp_path)
    out = tmp_path / "index"
    build(repo, out, backend="sqlite-vec", offline_embed=True)

    noop = update(repo, out, offline_embed=True)
    assert noop["embedded"] == 0 and noop["count"] == 4

    target = repo / "fact-order-fulfillment.yaml"
    target.write_text(target.read_text(encoding="utf-8").replace("jobType: eid", "jobType: eid-v2", 1))
    changed = update(repo, out, offline_embed=True)
    assert changed["changed"] == 1 and changed["count"] == 4


def test_update_bootstraps_when_index_missing(tmp_path) -> None:
    from pdc.search import update

    repo = _copy_samples(tmp_path)
    out = tmp_path / "index"
    result = update(repo, out, offline_embed=True)
    assert result["mode"] == "full"
    assert result["count"] == 4
