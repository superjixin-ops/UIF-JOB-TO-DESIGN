"""备份与恢复：企业里"数据丢了"是最贵的事故。

为什么不用 `cp jobs.db`：

    SQLite 在写入过程中复制，可能拿到一个**半写状态**的文件。
    正确做法是用 SQLite 自己的在线备份 API（Connection.backup），
    它会在事务边界上做快照，不需要停服务。

为什么带 manifest：

    备份文件坏了，你要在恢复的那一刻才知道——太晚了。
    manifest 里存每个文件的 sha256 和表行数，
    `verify` 可以在平时就发现"这份备份不可用"。

为什么要做恢复演练：

    没演练过的备份等于没有备份。
    `restore` 不是"把文件拷回去"，是走一遍真实的恢复路径，
    并且默认拒绝覆盖已有目录（防止手滑）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    out: dict[str, int] = {}
    for name in names:
        try:
            out[name] = int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])
        except sqlite3.Error:
            out[name] = -1
    return out


@dataclass
class BackupResult:
    path: Path
    files: list[dict]

    def to_dict(self) -> dict:
        return {"archive": str(self.path), "files": self.files}


def create(
    *,
    out_dir: str | Path = "backups",
    sources: list[str | Path] | None = None,
    keep: int = 7,
) -> BackupResult:
    """把若干 SQLite 文件打成一份带 manifest 的 tar.gz，并按数量轮转。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sources = [Path(p) for p in (sources or ["out/jobs.db", "out/audit.db"])]
    existing = [p for p in sources if p.exists()]
    if not existing:
        raise FileNotFoundError(f"没有可备份的数据库：{[str(p) for p in sources]}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = out / f"backup-{stamp}.tar.gz"
    files: list[dict] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for src in existing:
            dst = tmpdir / src.name
            # 在线备份：事务边界快照，不需要停服务。
            # 注意要显式 close：`with sqlite3.connect()` 只提交事务，不关连接，
            # 连接不关时 WAL 文件还在，会被一起打进归档。
            sconn = sqlite3.connect(src)
            dconn = sqlite3.connect(dst)
            try:
                sconn.backup(dconn)
                # 把 WAL 合并回主库并切回 DELETE 模式：
                # 否则 dst 可能带着 -wal/-shm，恢复时状态不确定。
                dconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dconn.execute("PRAGMA journal_mode=DELETE")
                counts = _table_counts(dconn)
            finally:
                sconn.close()
                dconn.close()
            for side in (f"{dst}-wal", f"{dst}-shm"):
                Path(side).unlink(missing_ok=True)
            files.append(
                {
                    "name": src.name,
                    "bytes": dst.stat().st_size,
                    "sha256": _sha256(dst),
                    "tables": counts,
                }
            )

        manifest = {
            "version": VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": files,
        }
        (tmpdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with tarfile.open(archive, "w:gz") as tar:
            for item in tmpdir.iterdir():
                tar.add(item, arcname=item.name)

    _rotate(out, keep)
    return BackupResult(archive, files)


def _rotate(out: Path, keep: int) -> None:
    if keep <= 0:
        return
    archives = sorted(out.glob("backup-*.tar.gz"))
    for old in archives[:-keep]:
        old.unlink(missing_ok=True)


def verify(archive: str | Path) -> dict:
    """校验哈希 + SQLite 完整性。返回报告，不抛异常（方便 cron 里用）。"""
    path = Path(archive)
    report: dict = {"archive": str(path), "ok": False, "files": [], "problems": []}
    if not path.exists():
        report["problems"].append("归档不存在")
        return report

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with tarfile.open(path, "r:gz") as tar:
                tar.extractall(tmpdir, filter="data")
            manifest_path = tmpdir / "manifest.json"
            if not manifest_path.exists():
                report["problems"].append("缺 manifest.json")
                return report
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            for item in manifest.get("files", []):
                f = tmpdir / item["name"]
                if not f.exists():
                    report["problems"].append(f"缺文件 {item['name']}")
                    continue
                actual = _sha256(f)
                if actual != item["sha256"]:
                    report["problems"].append(f"{item['name']} 哈希不符（文件被改过或损坏）")
                    continue
                with sqlite3.connect(f) as conn:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    counts = _table_counts(conn)
                if integrity != "ok":
                    report["problems"].append(f"{item['name']} integrity_check={integrity}")
                for table, expect in item.get("tables", {}).items():
                    got = counts.get(table, -1)
                    if got != expect:
                        report["problems"].append(f"{item['name']}.{table} 行数 {got} != {expect}")
                report["files"].append({"name": item["name"], "tables": counts})
    except (tarfile.TarError, OSError, json.JSONDecodeError) as exc:
        report["problems"].append(f"打不开归档：{exc}")
        return report

    report["ok"] = not report["problems"]
    return report


def restore(archive: str | Path, target: str | Path, *, force: bool = False) -> dict:
    """校验通过才恢复。默认不覆盖已有目录。"""
    target_dir = Path(target)
    if target_dir.exists() and any(target_dir.iterdir()) and not force:
        raise FileExistsError(f"{target_dir} 非空，加 --force 才会覆盖")

    report = verify(archive)
    if not report["ok"]:
        raise ValueError("备份校验不通过，拒绝恢复：" + "；".join(report["problems"]))

    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmpdir, filter="data")
        restored = []
        for f in tmpdir.iterdir():
            if f.name == "manifest.json" or not f.is_file():
                continue
            if f.name.endswith(("-wal", "-shm", ".journal")):
                continue  # 日志边车文件不恢复，主库已经 checkpoint 过
            shutil.copyfile(f, target_dir / f.name)
            restored.append(f.name)
    return {"restored": sorted(restored), "target": str(target_dir)}
