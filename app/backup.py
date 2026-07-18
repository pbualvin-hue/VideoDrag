"""Backups (CLAUDE.md rule 16): VACUUM INTO, 7-copy rotation, weekly
integrity check. Never copy the .db file directly under WAL mode.

Off-device replication (NAS / cloud via rclone) with mandatory encryption
is wired in when the user provides a destination — until then, backups
live under data/backups/ and the health panel reports them as local-only.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import Settings

logger = logging.getLogger(__name__)

# Serializes DB-file swaps (restore) against new connection opens (get_conn).
restore_lock = threading.Lock()

KEEP_COPIES = 7
BACKUP_EVERY_SECS = 24 * 3600
INTEGRITY_EVERY_SECS = 7 * 24 * 3600
_CHECK_INTERVAL = 600  # scheduler wake-up


def backups_dir(settings: Settings) -> Path:
    d = settings.data_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_backups(settings: Settings) -> list[Path]:
    return sorted(backups_dir(settings).glob("vidrag-*.db"), reverse=True)


def run_backup(settings: Settings) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backups_dir(settings) / f"vidrag-{stamp}.db"
    # Second-granularity stamps collide when two backups run in the same
    # second (e.g. restore's pre-snapshot right after a manual backup);
    # VACUUM INTO refuses to overwrite, so uniquify.
    n = 1
    while target.exists():
        target = backups_dir(settings) / f"vidrag-{stamp}-{n}.db"
        n += 1
    conn = db.connect(settings.db_path)
    try:
        # VACUUM INTO produces a consistent snapshot even under WAL.
        conn.execute("VACUUM INTO ?", (str(target),))
        db.set_meta(conn, "last_backup_at", db.utcnow_iso())
    finally:
        conn.close()
    for old in list_backups(settings)[KEEP_COPIES:]:
        old.unlink(missing_ok=True)
    logger.info("backup written: %s", target.name)
    return target


def rclone_error_message(stderr: str) -> str:
    """Translate an rclone failure into a human message (rule 18). The raw
    stderr can carry the NAS user@host and must not reach the user-facing
    HTTP response or the meta stored in plaintext .db backups; log it at
    debug only."""
    s = (stderr or "").lower()
    if any(k in s for k in ("connection refused", "no route", "timeout", "dial tcp",
                            "i/o timeout", "connect: ")):
        return "連不到 NAS(位址/port 錯誤,或 NAS 未開機/未開 SFTP)"
    if any(k in s for k in ("permission denied", "authentication", "unable to authenticate",
                            "auth", "password")):
        return "NAS 認證失敗(帳號或密碼錯誤)"
    if any(k in s for k in ("not found", "no such", "does not exist")):
        return "NAS 上找不到指定資料夾"
    return "異地備份失敗,請確認 NAS 設定(詳見伺服器 log)"


def _record_offsite(settings: Settings, ok: bool, err: str) -> None:
    conn = db.connect(settings.db_path)
    try:
        db.set_meta(conn, "last_offsite_at", db.utcnow_iso())
        db.set_meta(conn, "last_offsite_ok", "1" if ok else "0")
        db.set_meta(conn, "last_offsite_error", err)
    finally:
        conn.close()


def replicate_offsite(settings: Settings) -> None:
    """Mirror the local backups dir to the encrypted off-device remote
    (rule 16: off-device + mandatory encryption; the remote is an rclone
    `crypt` remote so files are encrypted before they leave the Pi).

    No-op until the user configures a remote. A failure here (NAS down, bad
    creds) must NOT break the local backup that already succeeded — but it is
    never swallowed: it is logged and recorded in meta so the health panel
    shows offsite as failing instead of silently stale (error-handling rule).
    """
    remote = settings.backup_remote
    if not remote:
        return
    if not settings.rclone_conf_path.is_file():
        _record_offsite(settings, False, "尚未完成異地備份設定(rclone.conf 不存在)")
        return
    try:
        result = subprocess.run(
            # remote is an rclone crypt remote whose base is the NAS backup
            # folder, so sync to its root — filenames land encrypted there.
            ["rclone", "--config", str(settings.rclone_conf_path), "sync",
             str(backups_dir(settings)), f"{remote}:",
             "--transfers", "2", "--timeout", "120s"],
            capture_output=True, text=True, timeout=600,
        )
    except FileNotFoundError:
        _record_offsite(settings, False, "容器內找不到 rclone")
        return
    except subprocess.TimeoutExpired:
        _record_offsite(settings, False, "異地上傳逾時(NAS 無回應?)")
        return
    if result.returncode == 0:
        _record_offsite(settings, True, "")
        logger.info("offsite replication ok -> %s", remote)
    else:
        _record_offsite(settings, False, rclone_error_message(result.stderr))
        logger.error("offsite replication failed (rc=%s)", result.returncode)
        logger.debug("rclone stderr: %s", (result.stderr or "").strip())


def run_integrity_check(settings: Settings) -> bool:
    conn = db.connect(settings.db_path)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.set_meta(conn, "last_integrity_at", db.utcnow_iso())
        db.set_meta(conn, "last_integrity_ok", "1" if ok else "0")
    finally:
        conn.close()
    if not ok:
        logger.error("PRAGMA integrity_check FAILED")
    return ok


def restore_backup(settings: Settings, backup_name: str) -> None:
    """Restore from a backup file. Safety: verify the candidate first, then
    snapshot current state (UX.md: 還原前自動再備份一次現況), then swap
    atomically. Caller must stop the worker first; the swap is serialized
    against new connection opens via restore_lock so no request opens a
    half-written db (review C2)."""
    # Reject path traversal: resolve and confirm the file sits directly in
    # the backups dir (a bare filename, not "../" or an absolute path).
    bdir = backups_dir(settings).resolve()
    src = (bdir / backup_name).resolve()
    if src.parent != bdir or not src.is_file():
        raise FileNotFoundError(f"備份不存在:{backup_name}")

    # Verify the candidate BEFORE the pre-backup rotation (which could
    # otherwise evict the very file being restored — review m1).
    probe = sqlite3.connect(src)
    try:
        if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("備份檔未通過完整性檢查,取消還原")
    finally:
        probe.close()
    run_backup(settings)  # snapshot current state before overwriting

    live = settings.db_path
    tmp = Path(str(live) + ".restore")
    tmp.unlink(missing_ok=True)
    probe = sqlite3.connect(src)
    try:
        probe.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        probe.close()
    # Gate new opens only for the swap; connections already in flight keep
    # their old inode on Linux and finish safely.
    with restore_lock:
        for suffix in ("-wal", "-shm"):
            Path(str(live) + suffix).unlink(missing_ok=True)
        os.replace(tmp, live)  # atomic on the same filesystem — no gap
    logger.info("restored from %s", backup_name)


def _elapsed(conn: sqlite3.Connection, meta_key: str) -> float:
    last = db.get_meta(conn, meta_key)
    if not last:
        return float("inf")
    dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


class BackupScheduler:
    """Daily backup + weekly integrity check, in-process."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tick(self) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            backup_due = _elapsed(conn, "last_backup_at") >= BACKUP_EVERY_SECS
            integrity_due = _elapsed(conn, "last_integrity_at") >= INTEGRITY_EVERY_SECS
        finally:
            conn.close()
        if backup_due:
            run_backup(self.settings)
            replicate_offsite(self.settings)
        if integrity_due:
            run_integrity_check(self.settings)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("backup scheduler tick failed")
            self._stop.wait(_CHECK_INTERVAL)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop,
                                        name="vidrag-backup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
