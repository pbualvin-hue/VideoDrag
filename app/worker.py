"""Single background worker (CLAUDE.md rule 7).

Polls the jobs table; on startup it recovers jobs stranded in `processing`
by a crash and sweeps orphaned temp audio files (rule 17). Claims are
atomic conditional UPDATEs so an accidental second worker cannot
double-run a job.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading

from . import db, errors
from .config import Settings
from .ingest.pipeline import ingest_podcast, ingest_text, ingest_url
from .rag.embedder import Embedder
from .rag.summarize import generate_summary

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 2.0


def enqueue_ingest(conn: sqlite3.Connection, url: str) -> int:
    now = db.utcnow_iso()
    cur = conn.execute(
        "INSERT INTO jobs (kind, payload, status, created_at, updated_at)"
        " VALUES ('ingest', ?, 'pending', ?, ?)",
        (json.dumps({"url": url}), now, now),
    )
    conn.commit()
    return cur.lastrowid


def enqueue_ingest_text(conn: sqlite3.Connection, text: str,
                        title: str | None = None) -> int:
    now = db.utcnow_iso()
    cur = conn.execute(
        "INSERT INTO jobs (kind, payload, status, created_at, updated_at)"
        " VALUES ('ingest_text', ?, 'pending', ?, ?)",
        (json.dumps({"text": text, "title": title}), now, now),
    )
    conn.commit()
    return cur.lastrowid


def enqueue_ingest_podcast(conn: sqlite3.Connection, episode: dict) -> int:
    now = db.utcnow_iso()
    cur = conn.execute(
        "INSERT INTO jobs (kind, payload, status, created_at, updated_at)"
        " VALUES ('ingest_podcast', ?, 'pending', ?, ?)",
        (json.dumps({"episode": episode}, ensure_ascii=False), now, now),
    )
    conn.commit()
    return cur.lastrowid


def recover_stale_jobs(conn: sqlite3.Connection) -> int:
    """Jobs left in `processing` by a crash: retry or fail them.

    Also resets sources stranded in `processing` (crash between the source
    INSERT and the failure handler) to `failed`, otherwise the dedup check
    would treat them as live duplicates and they'd stay permanently empty.
    """
    stale = conn.execute(
        "SELECT job_id, retries, max_retries FROM jobs WHERE status = 'processing'"
    ).fetchall()
    for job in stale:
        status = "pending" if job["retries"] < job["max_retries"] else "failed"
        conn.execute(
            "UPDATE jobs SET status = ?, retries = retries + 1, updated_at = ?,"
            " error = COALESCE(error, 'recovered after restart') WHERE job_id = ?",
            (status, db.utcnow_iso(), job["job_id"]),
        )
    conn.execute(
        "UPDATE sources SET status = 'failed' WHERE status = 'processing'"
    )
    # Startup sweep for text payloads that a crash left un-scrubbed in a
    # terminal job (done/duplicate/failed) — see scrub_text_payload.
    conn.execute(
        "UPDATE jobs SET payload = '{}' WHERE kind = 'ingest_text'"
        " AND status IN ('done', 'duplicate', 'failed') AND payload != '{}'"
    )
    conn.commit()
    if stale:
        logger.info("recovered %d stale processing jobs", len(stale))
    return len(stale)


def sweep_orphan_tmp(settings: Settings) -> None:
    tmp = settings.data_dir / "tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        logger.info("swept orphan temp files under %s", tmp)


def _claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    job = conn.execute(
        "SELECT job_id, kind, payload, source_id, retries, max_retries"
        " FROM jobs WHERE status = 'pending' ORDER BY job_id LIMIT 1"
    ).fetchone()
    if job is None:
        return None
    cur = conn.execute(
        "UPDATE jobs SET status = 'processing', updated_at = ?"
        " WHERE job_id = ? AND status = 'pending'",
        (db.utcnow_iso(), job["job_id"]),
    )
    conn.commit()
    return job if cur.rowcount else None


def _finish(conn: sqlite3.Connection, job_id: int, status: str,
            error: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
        (status, error, db.utcnow_iso(), job_id),
    )
    conn.commit()


def scrub_text_payload(conn: sqlite3.Connection, job_id: int) -> None:
    """Drop the pasted body from a terminal ingest_text job (security audit
    2026-07-14): the verbatim paste is personal content and must not outlive
    the job in jobs.payload — it would silently persist into every backup
    even after the source itself is deleted (rule 12-2 spirit)."""
    conn.execute(
        "UPDATE jobs SET payload = '{}' WHERE job_id = ?"
        " AND kind = 'ingest_text'", (job_id,),
    )
    conn.commit()


def _fail_or_retry(conn: sqlite3.Connection, job: sqlite3.Row, exc: Exception) -> None:
    retries = job["retries"] + 1
    # A permanent failure (e.g. a login wall) yields the same error on every
    # attempt — retrying only re-hits the remote site, so fail it immediately.
    permanent = errors.is_permanent(str(exc))
    status = "failed" if permanent or retries >= job["max_retries"] else "pending"
    conn.execute(
        "UPDATE jobs SET status = ?, retries = ?, error = ?, updated_at = ?"
        " WHERE job_id = ?",
        (status, retries, str(exc)[:4000], db.utcnow_iso(), job["job_id"]),
    )
    conn.commit()
    if status == "failed":
        scrub_text_payload(conn, job["job_id"])
    logger.warning("job %s -> %s: %s", job["job_id"], status, exc)


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._stop = threading.Event()
        self._embedder: Embedder | None = None
        self._thread: threading.Thread | None = None

    def _get_embedder(self, conn: sqlite3.Connection) -> Embedder:
        if self._embedder is None:
            model = db.get_meta(conn, "embedding_model") or self.settings.embedding_model
            self._embedder = Embedder(model)
            db.ensure_embedding_model(conn, self._embedder.model_name,
                                      self._embedder.dim)
        return self._embedder

    def _process(self, conn: sqlite3.Connection, job: sqlite3.Row) -> None:
        # Re-read settings per job (F6): the worker used to hold a boot-time
        # snapshot, so admin-page changes (jina toggle, key updates, budget)
        # silently didn't apply to ingest until a container restart. The API
        # layer already re-reads per request; the worker must match.
        from .config import load_settings
        settings = load_settings(require_keys=False)
        payload = json.loads(job["payload"])
        if job["kind"] in ("ingest", "ingest_text", "ingest_podcast"):
            if job["kind"] == "ingest":
                outcome = ingest_url(
                    payload["url"], settings, conn, self._get_embedder(conn)
                )
            elif job["kind"] == "ingest_podcast":
                outcome = ingest_podcast(
                    payload["episode"], settings, conn, self._get_embedder(conn)
                )
            else:
                outcome = ingest_text(
                    payload["text"], settings, conn,
                    self._get_embedder(conn), title=payload.get("title"),
                )
            conn.execute(
                "UPDATE jobs SET source_id = ? WHERE job_id = ?",
                (outcome.source_id, job["job_id"]),
            )
            _finish(conn, job["job_id"],
                    "duplicate" if outcome.status == "duplicate" else "done")
            # Terminal state: the pasted body has served its purpose.
            scrub_text_payload(conn, job["job_id"])
        elif job["kind"] == "summarize":
            generate_summary(conn, settings, payload["source_id"])
            _finish(conn, job["job_id"], "done")
        else:
            _finish(conn, job["job_id"], "failed",
                    f"unknown job kind: {job['kind']}")

    def _loop(self) -> None:
        conn = db.connect(self.settings.db_path)
        db.init_schema(conn)
        recover_stale_jobs(conn)
        sweep_orphan_tmp(self.settings)
        try:
            from .rag.summarize import backfill_display_titles
            backfill_display_titles(conn, self.settings)
        except Exception as exc:  # noqa: BLE001 — cosmetic, never blocks start
            logger.warning("display-title backfill error: %s", exc)
        logger.info("worker started")
        while not self._stop.is_set():
            job = _claim_next(conn)
            if job is None:
                self._stop.wait(POLL_INTERVAL_SECS)
                continue
            try:
                self._process(conn, job)
            except Exception as exc:
                conn.rollback()
                _fail_or_retry(conn, job, exc)
        conn.close()
        logger.info("worker stopped")

    def start(self) -> None:
        # Idempotent: never spawn a second worker, and clear the stop flag so
        # a worker paused for restore/update can actually resume (a prior
        # stop() leaves _stop set — without clearing it the new loop would
        # exit immediately and silently stall the queue).
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vidrag-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30) -> bool:
        """Signal the worker and wait. Returns False if it's still draining
        (a long ingest in flight) so callers can refuse unsafe file swaps."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("worker still draining after %ss", timeout)
                return False
        return True
