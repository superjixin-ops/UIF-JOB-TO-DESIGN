"""审计日志：谁、什么时候、做了什么，而且改不了。

为什么需要它：

    出问题时第一个问题是"这是谁操作的"。
    没有审计日志，你只有一堆 trace，无法回答"谁触发了这次跑批"。
    合规上（SOC2 / ISO 27001）这是必查项。

为什么不是"写个 JSONL 就完了"：

    JSONL 谁都能改，改完看不出来。
    这里每条记录带 `prev_hash`，用自己的内容 + 上一条的哈希算出 `hash`。
    改任何一条，后面全部对不上——**篡改可检测**。

    这不是区块链，也不需要分布式共识；
    一个带哈希链的 append-only 表就够挡住"事后改日志"。

不记录什么：

    token 明文、请求体全文、模型输出。
    只记 token_id（哈希前缀）、动作、目标、结果。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

AUDIT_ENV = "PDC_AUDIT_DB"
GENESIS = "0" * 64


def _canonical(payload: dict) -> str:
    """稳定序列化：同样的内容永远得到同样的字节。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AuditLog:
    def __init__(self, path: str | Path | None = None) -> None:
        raw = path or os.getenv(AUDIT_ENV, "out/audit.db")
        self.path = Path(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT 'ok',
                    ip TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    prev_hash TEXT NOT NULL,
                    hash TEXT NOT NULL
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_action ON events(action, seq)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def append(
        self,
        *,
        actor: str,
        role: str,
        action: str,
        target: str = "",
        result: str = "ok",
        ip: str = "",
        detail: str = "",
    ) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            payload = {
                "ts": round(time.time(), 3),
                "actor": actor,
                "role": role,
                "action": action,
                "target": target,
                "result": result,
                "ip": ip,
                "detail": detail,
            }
            digest = hashlib.sha256((prev + _canonical(payload)).encode()).hexdigest()
            cur = conn.execute(
                """INSERT INTO events(ts, actor, role, action, target, result, ip, detail, prev_hash, hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["ts"], actor, role, action, target, result, ip, detail, prev, digest,
                ),
            )
            return int(cur.lastrowid or 0)

    def list(self, *, limit: int = 50, offset: int = 0, action: str = "") -> list[dict]:
        sql = "SELECT * FROM events"
        params: list = []
        if action:
            sql += " WHERE action = ?"
            params.append(action)
        sql += " ORDER BY seq DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT count(*) FROM events").fetchone()[0])

    def verify(self) -> tuple[bool, int | None]:
        """从头重算哈希链。返回 (是否完整, 第一条坏记录的 seq)。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        prev = GENESIS
        for row in rows:
            payload = {
                "ts": row["ts"],
                "actor": row["actor"],
                "role": row["role"],
                "action": row["action"],
                "target": row["target"],
                "result": row["result"],
                "ip": row["ip"],
                "detail": row["detail"],
            }
            expect = hashlib.sha256((prev + _canonical(payload)).encode()).hexdigest()
            if row["prev_hash"] != prev or row["hash"] != expect:
                return False, int(row["seq"])
            prev = row["hash"]
        return True, None
