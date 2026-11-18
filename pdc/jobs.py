"""任务持久化：SQLite。

为什么不继续用内存 dict：
    重启就丢。用户刷新页面发现"任务不存在"，这在面试里是硬伤。

为什么是 SQLite 而不是 Postgres：
    单机、单进程、每天几十个任务。SQLite 零运维、单文件、可备份。
    等真到多实例再换，那时才需要连接池和迁移工具——现在上就是过度设计。

状态机：
    queued → running → succeeded / failed / cancelled
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    pipeline    TEXT NOT NULL,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    steps       INTEGER DEFAULT 0,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    files       TEXT DEFAULT '{}',
    issues      TEXT DEFAULT '[]',
    output      TEXT DEFAULT '',
    error       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
"""


@dataclass
class RunRecord:
    id: str
    pipeline: str
    mode: str
    status: str
    created_at: float
    updated_at: float
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    files: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    output: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.id,
            "pipeline": self.pipeline,
            "mode": self.mode,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": self.steps,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "files": self.files,
            "issues": self.issues,
            "output": self.output,
            "error": self.error,
        }


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    def create(self, run_id: str, pipeline: str, mode: str) -> RunRecord:
        now = time.time()
        with closing(self._conn()) as conn:
            conn.execute(
                "INSERT INTO runs (id, pipeline, mode, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?)",
                (run_id, pipeline, mode, now, now),
            )
            conn.commit()
        return RunRecord(run_id, pipeline, mode, "queued", now, now)

    def update(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status", "steps", "tokens_in", "tokens_out",
            "files", "issues", "output", "error",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"不允许更新字段: {key}")
            if key in ("files", "issues"):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key} = ?")
            values.append(value)
        sets.append("updated_at = ?")
        values.append(time.time())
        values.append(run_id)
        with closing(self._conn()) as conn:
            conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", values)
            conn.commit()

    # ------------------------------------------------------------------
    def get(self, run_id: str) -> RunRecord | None:
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list_recent(self, limit: int = 20) -> list[RunRecord]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def mark_orphans(self) -> int:
        """进程重启后，把卡在 queued/running 的任务标成 failed。

        否则用户永远看到一个转圈的"运行中"。
        """
        with closing(self._conn()) as conn:
            cur = conn.execute(
                "UPDATE runs SET status='failed', error='服务重启，任务中断', updated_at=? "
                "WHERE status IN ('queued','running')",
                (time.time(),),
            )
            conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            pipeline=row["pipeline"],
            mode=row["mode"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            steps=row["steps"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            files=json.loads(row["files"] or "{}"),
            issues=json.loads(row["issues"] or "[]"),
            output=row["output"] or "",
            error=row["error"] or "",
        )
