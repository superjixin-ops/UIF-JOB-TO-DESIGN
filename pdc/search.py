"""语义检索：建索引、查索引、比后端。

三个命令对应三件事：

    pdc index build --repo samples/pipelines --out out/index
    pdc search "处理订单履约的流水线" --index out/index
    pdc bench --index out/index

默认后端是 numpy：0 依赖、索引目录可以直接拷走。
换 sqlite-vec 只改一个参数，业务代码不动。
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

import numpy as np

from pdc.corpus_index import build_cards, read_cards, write_cards
from pdc.embed import get_embedder
from pdc.vectorstore import Hit, JsonlStore, NumpyStore, PgVectorStore, SqliteVecStore

META = "meta.json"

# 语料是英文（YAML 标识符），查询往往是中文。
# bge-small-zh 在英文文本上跨语言能力弱：实测 "订单履约的流水线" 命不中
# fact-order-fulfillment，而 "order fulfillment fact pipeline" 排第一。
#
# 这是最小桥：把高频业务词补成英文再 embed。
# 它不是"聪明"的做法——正式方案是换多语模型（PDC_EMBED_MODEL）。
# 保留它是因为：确定性、可测试、零成本，而且效果能量化。
QUERY_GLOSSARY = {
    "订单": "order",
    "履约": "fulfillment",
    "库存": "inventory",
    "快照": "snapshot",
    "客户": "customer",
    "产品": "product",
    "维度": "dimension",
    "事实": "fact",
    "流水线": "pipeline",
    "抽取": "extract",
    "落地": "landing",
    "汇总": "aggregate",
    "分区": "partition",
    "增量": "incremental",
    "全量": "full load",
    "合同": "contract",
    "发票": "invoice",
    "发货": "shipment",
    "退货": "return",
    "销售": "sales",
    "营销": "marketing",
    "供应商": "supplier",
    "仓库": "warehouse",
    "员工": "employee",
}


def expand_query(query: str) -> str:
    """中文关键词补英文。已经是英文的查询不受影响。"""
    extra = [en for zh, en in QUERY_GLOSSARY.items() if zh in query]
    return query if not extra else f"{query} {' '.join(extra)}"


def build(
    repo: str | Path,
    out: str | Path,
    *,
    backend: str = "numpy",
    offline_embed: bool = False,
    limit: int | None = None,
    dsn: str = "",
) -> dict:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cards = build_cards(repo, limit=limit)
    if not cards:
        raise SystemExit(f"在 {repo} 里没有找到可用的 pipeline")
    write_cards(cards, out_dir / "cards.jsonl")

    embedder = get_embedder(offline=offline_embed)
    vectors = embedder.encode([c.text for c in cards])
    ids = [c.id for c in cards]
    metas = [c.meta for c in cards]

    if backend == "sqlite-vec":
        db = out_dir / "index.db"
        db.unlink(missing_ok=True)
        store = SqliteVecStore(db, dim=int(vectors.shape[1]))
        store.add(list(zip(ids, vectors, metas, strict=True)))
        store.close()
    elif backend == "pgvector":
        conn = dsn or os.getenv("PDC_PGVECTOR_DSN", "")
        if not conn:
            raise SystemExit("pgvector 需要 --dsn 或环境变量 PDC_PGVECTOR_DSN")
        store = PgVectorStore(conn, dim=int(vectors.shape[1]))
        store.reset()  # 全量重建：清空旧数据
        store.add(list(zip(ids, vectors, metas, strict=True)))
        store.close()
    elif backend == "numpy":
        JsonlStore(out_dir).save(ids, vectors, metas)
    else:
        raise SystemExit(f"未知后端: {backend}")

    meta = {
        "backend": backend,
        "embedder": embedder.name,
        "dim": int(vectors.shape[1]),
        "count": len(cards),
        "repo": str(repo),
    }
    (out_dir / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def update(
    repo: str | Path,
    out: str | Path,
    *,
    backend: str = "",
    offline_embed: bool = False,
    limit: int | None = None,
    dsn: str = "",
) -> dict:
    """增量更新索引：只 embed 新增和变更的条目。

    为什么这条命令比 build 重要：
        全量重建 1,000 条要 5 分钟（embedding 是瓶颈）。
        每天改几条 pipeline，重算全部是纯浪费。
        增量更新把"日常维护"从分钟级降到秒级——
        这正是向量库相对于 numpy 的卖点，不做这一步，卖点只是口号。

    判定"变了"用卡片文本，不用文件 mtime：
        mtime 会因为 checkout/pull 变化，但内容没变，会造成误重算。
    """
    import time

    d = Path(out)
    meta_path = d / META
    if not meta_path.exists():
        # 没有旧索引就等价于全量构建
        meta = build(repo, out, backend=backend or "numpy", offline_embed=offline_embed, limit=limit)
        return {"mode": "full", "embedded": meta["count"], "added": meta["count"],
                "changed": 0, "removed": 0, "count": meta["count"], "seconds": 0.0}

    t0 = time.perf_counter()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    backend = backend or meta["backend"]

    old = {c["id"]: c for c in read_cards(d / "cards.jsonl")}
    new = {c.id: c for c in build_cards(repo, limit=limit)}

    added = [i for i in new if i not in old]
    removed = [i for i in old if i not in new]
    changed = [
        i for i in new
        if i in old and new[i].text != old[i].get("text", "")
    ]
    to_embed = added + changed

    embedder = get_embedder(offline=offline_embed)
    vectors = embedder.encode([new[i].text for i in to_embed]) if to_embed else None

    # 卡片文件先整体重写：它只是展示用，重建成本可以忽略
    write_cards(list(new.values()), d / "cards.jsonl")

    if backend == "pgvector":
        conn = dsn or os.getenv("PDC_PGVECTOR_DSN", "")
        if not conn:
            raise SystemExit("pgvector 需要 --dsn 或环境变量 PDC_PGVECTOR_DSN")
        store = PgVectorStore(conn, dim=meta["dim"])
        store.delete(removed)
        if to_embed:
            store.add([(i, vectors[k], new[i].meta) for k, i in enumerate(to_embed)])
        count = store.count()
        store.close()
    elif backend == "sqlite-vec":
        store = SqliteVecStore(d / "index.db", dim=meta["dim"])
        store.delete(removed)
        if to_embed:
            store.add([
                (i, vectors[k], new[i].meta) for k, i in enumerate(to_embed)
            ])
        count = store.count()
        store.close()
    else:
        rows = read_cards(d / "index.jsonl") if (d / "index.jsonl").exists() else []
        matrix = np.load(d / "vectors.npy") if (d / "vectors.npy").exists() else None
        by_id = {}
        if rows and matrix is not None:
            for row, vec in zip(rows, matrix, strict=True):
                by_id[row["id"]] = (vec, {k: v for k, v in row.items() if k != "id"})
        for i in removed:
            by_id.pop(i, None)
        for k, i in enumerate(to_embed):
            by_id[i] = (vectors[k], new[i].meta)
        ids = list(by_id)
        JsonlStore(d).save(ids, np.vstack([by_id[i][0] for i in ids]), [by_id[i][1] for i in ids])
        count = len(ids)

    meta.update({"backend": backend, "count": count, "embedder": embedder.name})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "mode": "incremental",
        "backend": backend,
        "added": len(added),
        "changed": len(changed),
        "removed": len(removed),
        "embedded": len(to_embed),
        "count": count,
        "seconds": round(time.perf_counter() - t0, 2),
    }


def load_store(index_dir: str | Path, dsn: str = ""):
    d = Path(index_dir)
    meta_path = d / META
    if not meta_path.exists():
        raise SystemExit(f"{d} 不是索引目录（缺 {META}）。先跑 pdc index build")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta["backend"] == "sqlite-vec":
        store = SqliteVecStore(d / "index.db", dim=meta["dim"])
    elif meta["backend"] == "pgvector":
        conn = dsn or os.getenv("PDC_PGVECTOR_DSN", "")
        if not conn:
            raise SystemExit("这个索引用的是 pgvector，需要 --dsn 或 PDC_PGVECTOR_DSN")
        store = PgVectorStore(conn, dim=meta["dim"])
    else:
        store = JsonlStore(d).load()
    return store, meta


def search(
    index_dir: str | Path,
    query: str,
    *,
    k: int = 5,
    where: dict | None = None,
    offline_embed: bool = False,
    dsn: str = "",
) -> list[Hit]:
    store, _meta = load_store(index_dir, dsn=dsn)
    embedder = get_embedder(offline=offline_embed)
    vector = embedder.encode([expand_query(query)])[0]
    return store.search(vector, k=k, where=where)


def bench(index_dir: str | Path, sizes: list[int], queries: int = 20) -> dict:
    """同一份向量，比 numpy 和 sqlite-vec 的构建时间与查询延迟。

    规模靠复制 + 微扰造出来——测的是"索引结构"的成本，
    不是"语义质量"。语义质量由 pdc search 的评测集负责。
    """
    store, meta = load_store(index_dir)
    if isinstance(store, SqliteVecStore):
        store.close()

    cards = read_cards(Path(index_dir) / "cards.jsonl")
    dim = meta["dim"]
    base = np.load(Path(index_dir) / "vectors.npy") if (Path(index_dir) / "vectors.npy").exists() else None
    if base is None:
        raise SystemExit("bench 需要 numpy 索引目录（vectors.npy）；先用 --backend numpy 建一次")
    rng = np.random.default_rng(0)

    rows = []
    for n in sizes:
        if n <= len(base):
            vectors = base[:n]
            ids = [c["id"] for c in cards][:n]
            metas = [{"chain": c["chain"], "kind": c["kind"], "path": c["path"]} for c in cards][:n]
        else:
            reps = int(np.ceil(n / len(base)))
            vectors = np.tile(base, (reps, 1))[:n] + rng.normal(0, 0.001, (n, dim)).astype(np.float32)
            ids = [f"{c['id']}#{i}" for i, c in enumerate(cards * reps)][:n]
            metas = [
                {"chain": c["chain"], "kind": c["kind"], "path": c["path"]}
                for c in (cards * reps)
            ][:n]

        t0 = time.perf_counter()
        np_store = NumpyStore()
        np_store.add(list(zip(ids, vectors, metas, strict=True)))
        np_build = time.perf_counter() - t0

        t0 = time.perf_counter()
        sv_store = SqliteVecStore(Path(index_dir) / f"bench-{n}.db", dim=dim)
        sv_store.add(list(zip(ids, vectors, metas, strict=True)))
        sv_build = time.perf_counter() - t0

        probes = [vectors[int(i)] for i in rng.integers(0, len(vectors), queries)]

        def timed(fn, probes_local) -> float:
            samples = []
            for q in probes_local:
                t = time.perf_counter()
                fn(q)
                samples.append((time.perf_counter() - t) * 1000)
            return statistics.median(samples)

        rows.append(
            {
                "n": n,
                "numpy_build_s": round(np_build, 2),
                "sqlite_build_s": round(sv_build, 2),
                "numpy_p50_ms": round(timed(lambda q, st=np_store: st.search(q, k=5), probes), 2),
                "sqlite_p50_ms": round(timed(lambda q, st=sv_store: st.search(q, k=5), probes), 2),
                "db_size_mb": round(Path(sv_store.path).stat().st_size / 1e6, 1),
            }
        )
        sv_store.close()
        Path(sv_store.path).unlink(missing_ok=True)

    return {"queries": queries, "rows": rows}
