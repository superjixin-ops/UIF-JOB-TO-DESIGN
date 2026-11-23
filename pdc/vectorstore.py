"""向量索引：同一个接口，两个后端。

为什么做抽象：

    选型不该写死在业务代码里。默认用 numpy（0 依赖、单文件），
    需要增量更新 / 多进程共享 / 元数据过滤时换 sqlite-vec，
    业务代码一行不改。

    **这也是"什么时候该上向量库"那个问题的可执行版本**：
    两个后端跑同一套数据、同一套查询，谁快谁慢用数字说话。

两个后端的定位：

    numpy        暴力扫描。10 万条 512 维大约几十毫秒，够用。
    sqlite-vec   真·向量库（SQLite 扩展），支持持久化、增量写、分区过滤。
                 单文件，不引入新服务——保住现在的部署形态。
    pgvector     已经有 Postgres 时的选择。一个库同时存业务数据和向量，
                 支持 HNSW 索引和多实例共享。

注意：sqlite-vec 的元数据过滤只支持 partition key。
所以这里把 chain 设成分区键（最常用的过滤维度），其余字段放 meta。
任意字段过滤请用 numpy 后端，或者先按分区缩小范围。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np


@dataclass
class Hit:
    id: str
    score: float
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "score": round(self.score, 4), "metadata": self.metadata}


class VectorStore(Protocol):
    name: str

    def add(self, items: list[tuple[str, np.ndarray, dict]]) -> None: ...

    def search(self, vector: np.ndarray, k: int = 5, where: dict | None = None) -> list[Hit]: ...

    def count(self) -> int: ...


def _normalize(matrix: np.ndarray) -> np.ndarray:
    """按行归一化。零向量的范数当 1，避免除零。"""
    norm = np.linalg.norm(matrix, axis=-1, keepdims=True)
    norm[norm == 0] = 1.0
    return matrix / norm


class NumpyStore:
    """暴力扫描。适合 < 10 万条，以及"不想引入任何依赖"的场景。"""

    name = "numpy"

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._meta: list[dict] = []
        self._rows: list[np.ndarray] = []

    def add(self, items: list[tuple[str, np.ndarray, dict]]) -> None:
        for item_id, vector, meta in items:
            self._ids.append(str(item_id))
            self._meta.append(dict(meta))
            self._rows.append(np.asarray(vector, dtype=np.float32))

    def search(self, vector: np.ndarray, k: int = 5, where: dict | None = None) -> list[Hit]:
        if not self._rows:
            return []
        matrix = _normalize(np.vstack(self._rows))
        query = _normalize(np.asarray(vector, dtype=np.float32).reshape(1, -1))[0]
        sims = matrix @ query
        order = np.argsort(-sims)
        out: list[Hit] = []
        for idx in order:
            meta = self._meta[idx]
            if where and any(meta.get(key) != value for key, value in where.items()):
                continue
            out.append(Hit(self._ids[idx], float(sims[idx]), meta))
            if len(out) >= k:
                break
        return out

    def count(self) -> int:
        return len(self._ids)


class SqliteVecStore:
    """sqlite-vec 扩展。单文件、可增量、支持分区过滤。"""

    name = "sqlite-vec"

    def __init__(self, path: str | Path, dim: int) -> None:
        import sqlite_vec

        self.path = str(path)
        self.dim = int(dim)
        self._conn = sqlite3.connect(self.path)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS items USING vec0(
                    id TEXT PRIMARY KEY,
                    embedding float[{self.dim}],
                    chain TEXT partition key,
                    +meta TEXT
                )"""
        )
        self._conn.commit()

    def add(self, items: list[tuple[str, np.ndarray, dict]]) -> None:
        for item_id, vector, meta in items:
            key = str(item_id)
            blob = _normalize(np.asarray(vector, dtype=np.float32).reshape(1, -1))[0].tobytes()
            # vec0 没有 upsert，先删再插
            self._conn.execute("DELETE FROM items WHERE id = ?", (key,))
            self._conn.execute(
                "INSERT INTO items(id, embedding, chain, meta) VALUES (?, ?, ?, ?)",
                (key, blob, str(meta.get("chain", "")), json.dumps(meta, ensure_ascii=False)),
            )
        self._conn.commit()

    def search(self, vector: np.ndarray, k: int = 5, where: dict | None = None) -> list[Hit]:
        query = _normalize(np.asarray(vector, dtype=np.float32).reshape(1, -1))[0].tobytes()
        sql = "SELECT id, distance, meta FROM items WHERE embedding MATCH ? AND k = ?"
        params: list = [query, k * 4]
        if where and "chain" in where:
            sql += " AND chain = ?"
            params.append(str(where["chain"]))
        rows = self._conn.execute(sql, params).fetchall()

        out: list[Hit] = []
        for item_id, distance, meta_json in rows:
            meta = json.loads(meta_json) if meta_json else {}
            if where and any(meta.get(key) != value for key, value in where.items()):
                continue
            # 归一化向量的 L2 距离与余弦的关系：cos = 1 - d²/2
            out.append(Hit(item_id, 1.0 - float(distance) ** 2 / 2, meta))
            if len(out) >= k:
                break
        return out

    def reset(self) -> None:
        """全量重建时清空。

        注意：sqlite-vec 的 vec0 是虚拟表，没有 TRUNCATE，也没有自增序列可重置；
        这里是 SQLite 不是 Postgres，只能 DELETE。
        """
        self._conn.execute("DELETE FROM items")
        self._conn.commit()

    def delete(self, ids: list[str]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = self._conn.execute(f"DELETE FROM items WHERE id IN ({marks})", ids)
        self._conn.commit()
        return int(cur.rowcount or 0)

    def count(self) -> int:
        row = self._conn.execute("SELECT count(*) FROM items").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()


def _vec_literal(vector: np.ndarray) -> str:
    """pgvector 接受的文本格式：[0.1,0.2,...]。

    不用 psycopg 的 list 适配器：它会把 list 映射成 float[]，
    再加 ::vector 转换也能用，但多一层隐式转换，报错信息更难懂。
    """
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    return "[" + ",".join(f"{x:.6f}" for x in arr) + "]"


class PgVectorStore:
    """pgvector：Postgres 扩展，适合"已经有 PG"的环境。

    和 sqlite-vec 的取舍：

        sqlite-vec   零服务、单文件、适合单机
        pgvector     多实例共享、和业务表同库、支持 HNSW + 复杂过滤

    什么时候该换：多副本部署、需要按用户/租户过滤、需要事务里同时写向量和业务数据。
    """

    name = "pgvector"

    def __init__(self, dsn: str, dim: int, table: str = "pdc_vectors") -> None:
        import re

        import psycopg

        if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
            raise ValueError(f"非法表名: {table!r}")
        self.dsn = dsn
        self.dim = int(dim)
        self.table = table
        self._conn = psycopg.connect(dsn, connect_timeout=10)
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                    id TEXT PRIMARY KEY,
                    embedding vector({self.dim}),
                    chain TEXT NOT NULL DEFAULT '',
                    meta JSONB NOT NULL DEFAULT '{{}}'
                )"""
            )
            # HNSW：近似最近邻。数据量小时可以不加，但加了才有"向量库"的意义。
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {table}_hnsw "
                f"ON {table} USING hnsw (embedding vector_cosine_ops)"
            )

    def add(self, items: list[tuple[str, np.ndarray, dict]]) -> None:
        rows = [
            (str(i), _vec_literal(v), str(m.get("chain", "")), json.dumps(m, ensure_ascii=False))
            for i, v, m in items
        ]
        with self._conn.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO {self.table}(id, embedding, chain, meta)
                    VALUES (%s, %s::vector, %s, %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        chain = EXCLUDED.chain,
                        meta = EXCLUDED.meta""",
                rows,
            )

    def search(self, vector: np.ndarray, k: int = 5, where: dict | None = None) -> list[Hit]:
        lit = _vec_literal(vector)
        sql = (
            f"SELECT id, 1 - (embedding <=> %s::vector) AS score, meta "
            f"FROM {self.table}"
        )
        params: list = [lit]
        if where and "chain" in where:
            sql += " WHERE chain = %s"
            params.append(str(where["chain"]))
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.extend([lit, k])

        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        out: list[Hit] = []
        for item_id, score, meta in rows:
            data = meta if isinstance(meta, dict) else json.loads(meta)
            if where and any(data.get(key) != value for key, value in where.items()):
                continue
            out.append(Hit(str(item_id), float(score), data))
        return out

    def reset(self) -> None:
        """全量重建时清空。TRUNCATE 比 DELETE 快，且重置自增序列。"""
        with self._conn.cursor() as cur:
            cur.execute(f"TRUNCATE {self.table}")

    def delete(self, ids: list[str]) -> int:
        if not ids:
            return 0
        with self._conn.cursor() as cur:
            cur.execute(f"DELETE FROM {self.table} WHERE id = ANY(%s)", (ids,))
            return int(cur.rowcount or 0)

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {self.table}")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()


class JsonlStore:
    """把向量和元数据存成两个文件，加载时构建 NumpyStore。

    这是 numpy 后端的持久化方式：不引入数据库，index 目录可以整个拷走。
    """

    name = "numpy-jsonl"

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def save(self, ids: list[str], vectors: np.ndarray, metas: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        # 注意不要写 cards.jsonl：那份文件由 corpus_index 写，含卡片正文，
        # 两边同时写会互相覆盖（踩过）。
        (self.dir / "index.jsonl").write_text(
            "\n".join(
                json.dumps({"id": i, **m}, ensure_ascii=False) for i, m in zip(ids, metas, strict=True)
            )
            + "\n",
            encoding="utf-8",
        )
        np.save(self.dir / "vectors.npy", _normalize(np.asarray(vectors, dtype=np.float32)))

    def load(self) -> NumpyStore:
        rows = [
            json.loads(line)
            for line in (self.dir / "index.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        vectors = np.load(self.dir / "vectors.npy")
        store = NumpyStore()
        for row, vector in zip(rows, vectors, strict=True):
            item_id = row.pop("id")
            store.add([(item_id, vector, row)])
        return store
