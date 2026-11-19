"""企业级能力的测试：配置、RBAC、审计、指标、备份。

这五件事的共同点：**出错时往往没有报错，只是行为不对**。
所以每条都要有一个"故意弄坏它"的用例。
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pdc import backup
from pdc.audit_log import AuditLog
from pdc.auth import Principal, require_role
from pdc.config import Settings, parse_tokens
from pdc.metrics import Metrics, percentile_from_hist


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_config_reports_all_problems_at_once(monkeypatch) -> None:
    """配置错误要一次报全，而不是让人重启五次。"""
    monkeypatch.setenv("PDC_PORT", "not-a-port")
    monkeypatch.setenv("PDC_MAX_CONCURRENCY", "0")
    monkeypatch.setenv("PDC_RAG_MODE", "magic")
    settings = Settings.load()
    text = " ".join(settings.problems)
    assert "PDC_PORT" in text
    assert "PDC_MAX_CONCURRENCY" in text
    assert "PDC_RAG_MODE" in text


def test_token_parser_rejects_short_and_duplicate() -> None:
    tokens, problems = parse_tokens("short:admin,aaaaaaaaaaaaaaaa:admin,aaaaaaaaaaaaaaaa:viewer")
    assert tokens == {"aaaaaaaaaaaaaaaa": "admin"}
    assert any("太短" in p for p in problems)
    assert any("重复" in p for p in problems)


def test_token_parser_rejects_unknown_role() -> None:
    _, problems = parse_tokens("aaaaaaaaaaaaaaaa:root")
    assert any("不认识" in p for p in problems)


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
def _rbac_client(monkeypatch, tokens: str) -> TestClient:
    monkeypatch.setenv("PDC_TOKENS", tokens)
    app = FastAPI()

    @app.get("/read")
    def read(p: Principal = Depends(require_role("viewer"))) -> dict:  # noqa: B008
        return {"role": p.role}

    @app.post("/write")
    def write(p: Principal = Depends(require_role("operator"))) -> dict:  # noqa: B008
        return {"role": p.role}

    @app.get("/admin")
    def admin(p: Principal = Depends(require_role("admin"))) -> dict:  # noqa: B008
        return {"role": p.role}

    return TestClient(app)


TOKENS = "viewer-token-0001:viewer,operator-token-01:operator,admin-token-00001:admin"


def test_rbac_matrix(monkeypatch) -> None:
    client = _rbac_client(monkeypatch, TOKENS)
    viewer = {"Authorization": "Bearer viewer-token-0001"}
    operator = {"Authorization": "Bearer operator-token-01"}
    admin = {"Authorization": "Bearer admin-token-00001"}

    assert client.get("/read", headers=viewer).status_code == 200
    assert client.post("/write", headers=viewer).status_code == 403
    assert client.post("/write", headers=operator).status_code == 200
    assert client.get("/admin", headers=operator).status_code == 403
    assert client.get("/admin", headers=admin).status_code == 200
    assert client.get("/read").status_code == 401


def test_role_hierarchy() -> None:
    assert Principal("x", "admin", "env").can("operator")
    assert Principal("x", "operator", "env").can("viewer")
    assert not Principal("x", "viewer", "env").can("operator")


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.db")
    for i in range(3):
        log.append(actor="t1", role="operator", action="run.create", target=f"p{i}")
    assert log.verify() == (True, None)

    # 直接改库——模拟"有人事后改日志"
    with sqlite3.connect(tmp_path / "audit.db") as conn:
        conn.execute("UPDATE events SET target = 'tampered' WHERE seq = 2")

    ok, bad_seq = log.verify()
    assert not ok
    assert bad_seq == 2


def test_token_id_is_hashed_not_raw() -> None:
    """审计里存的是哈希前缀，不是 token 本身。

    这条比"库里有没有明文"更可靠：WAL 模式下数据先写 -wal 文件，
    直接读主库文件会漏（这次就踩了）。
    """
    from pdc.auth import token_id

    raw = "super-secret-token-value"
    tid = token_id(raw)
    assert tid != raw
    assert len(tid) == 12
    assert token_id(raw) == tid  # 稳定，可用来关联同一个人
    assert token_id("another-token") != tid


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def test_metrics_render_has_types_and_labels() -> None:
    m = Metrics()
    m.inc("pdc_http_requests_total", {"path": "/runs", "status": "200"})
    m.set("pdc_queue_depth", 2)
    m.observe("pdc_http_request_duration_seconds", 0.03, {"path": "/runs"})
    text = m.render()
    assert 'pdc_http_requests_total{path="/runs",status="200"} 1' in text
    assert "pdc_queue_depth 2" in text
    assert "pdc_http_request_duration_seconds_bucket" in text


def test_percentile_from_hist() -> None:
    m = Metrics()
    for v in [0.01] * 90 + [5.0] * 10:
        m.observe("lat", v)
    p95 = percentile_from_hist(m.render(), "lat", 0.95)
    assert p95 >= 5.0  # p95 落在 5s 桶


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t(v) VALUES (?)", [("a",), ("b",)])


def test_backup_verify_and_restore(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _make_db(db)

    result = backup.create(out_dir=tmp_path / "bk", sources=[db], keep=3)
    report = backup.verify(result.path)
    assert report["ok"], report["problems"]

    target = tmp_path / "restored"
    backup.restore(result.path, target)
    with sqlite3.connect(target / "jobs.db") as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 2


def test_backup_verify_detects_corruption(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _make_db(db)
    result = backup.create(out_dir=tmp_path / "bk", sources=[db])

    # 解包 -> 改库 -> 重新打包
    work = tmp_path / "work"
    work.mkdir()
    with tarfile.open(result.path, "r:gz") as tar:
        tar.extractall(work, filter="data")
    (work / "jobs.db").write_bytes(b"not a database")
    broken = tmp_path / "broken.tar.gz"
    with tarfile.open(broken, "w:gz") as tar:
        for item in work.iterdir():
            tar.add(item, arcname=item.name)

    report = backup.verify(broken)
    assert not report["ok"]
    assert any("哈希不符" in p or "integrity" in p for p in report["problems"])


def test_restore_refuses_non_empty_target(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _make_db(db)
    result = backup.create(out_dir=tmp_path / "bk", sources=[db])
    target = tmp_path / "restored"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):
        backup.restore(result.path, target)


def test_backup_manifest_counts_rows(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    _make_db(db)
    result = backup.create(out_dir=tmp_path / "bk", sources=[db])
    assert result.files[0]["tables"]["t"] == 2


def test_backup_archive_excludes_wal_sidecars(tmp_path: Path) -> None:
    """归档里不能有 -wal / -shm。

    它们是 SQLite 的日志边车文件，跟着主库一起恢复会让状态不确定。
    第一次实现就带上了，因为 `with sqlite3.connect()` 不会关闭连接。
    """
    db = tmp_path / "jobs.db"
    _make_db(db)
    result = backup.create(out_dir=tmp_path / "bk", sources=[db])
    with tarfile.open(result.path, "r:gz") as tar:
        names = tar.getnames()
    assert not any(n.endswith(("-wal", "-shm")) for n in names)
    assert "manifest.json" in names
