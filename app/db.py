"""SQLite storage layer: schema, connection pragmas, meta model-version gate.

Conventions (CLAUDE.md rules 5/12-2/14):
- WAL mode + busy_timeout on every connection
- all timestamps stored as UTC ISO-8601 strings, converted for display only
- vectors live in a sqlite-vec vec0 table; only source-content chunks,
  AI answers must never be embedded
- naming centers on `sources` (multi-type knowledge base), not videos
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id       INTEGER PRIMARY KEY,
    type            TEXT NOT NULL DEFAULT 'video',      -- video / article / text
    platform        TEXT NOT NULL,                      -- youtube / instagram / tiktok / web / manual
    video_id        TEXT NOT NULL,                      -- platform-native content id
    title           TEXT,                               -- original source title (attribution)
    display_title   TEXT,                               -- AI topic name shown in UI
    url_original    TEXT NOT NULL,
    url_normalized  TEXT NOT NULL,
    published_at    TEXT,                               -- UTC ISO-8601
    duration_secs   INTEGER,
    ingested_at     TEXT NOT NULL,                      -- UTC ISO-8601
    status          TEXT NOT NULL DEFAULT 'processing', -- processing/ready/enriched/failed
    summary         TEXT,
    summary_generated_at TEXT,
    UNIQUE (platform, video_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    modality  TEXT NOT NULL DEFAULT 'audio',            -- audio / visual / article
    text      TEXT NOT NULL,
    start_sec REAL,
    end_sec   REAL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);

CREATE TABLE IF NOT EXISTS messages (
    msg_id          INTEGER PRIMARY KEY,
    session_id      TEXT NOT NULL,
    role            TEXT NOT NULL,                      -- user / assistant
    content         TEXT NOT NULL,
    cited_chunk_ids TEXT,                               -- JSON array
    retrieval_trace TEXT,                               -- JSON: ranked chunks + two-path scores (gap-3)
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, msg_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id      INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                          -- ingest / ingest_text / summarize
    payload     TEXT NOT NULL,                          -- JSON
    status      TEXT NOT NULL DEFAULT 'pending',        -- pending/processing/done/failed/duplicate
    retries     INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    source_id   INTEGER REFERENCES sources(source_id) ON DELETE SET NULL,
    error       TEXT,                                   -- raw error, internal only
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, job_id);

-- Phase C (PLAN-DISTILL.md): AI-distilled notes live in their own tables,
-- NEVER in chunks/chunk_vectors — rule 12 keeps the source RAG free of AI
-- output. Candidates await user approval; only kept notes get embedded.
CREATE TABLE IF NOT EXISTS notes (
    note_id     INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                      -- skill/rule/project/money/insight
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,                      -- distilled insight (AI-generated)
    application TEXT,                               -- 適用情境+應用方式+要不要先做
    source_ids  TEXT,                               -- JSON array, provenance
    status      TEXT NOT NULL DEFAULT 'candidate',  -- candidate/kept/dropped
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status, note_id);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    title, content, application,
    content='notes',
    content_rowid='note_id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, content, application)
    VALUES (new.note_id, new.title, new.content, new.application);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, application)
    VALUES ('delete', old.note_id, old.title, old.content, old.application);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, content, application)
    VALUES ('delete', old.note_id, old.title, old.content, old.application);
    INSERT INTO notes_fts(rowid, title, content, application)
    VALUES (new.note_id, new.title, new.content, new.application);
END;

-- User-defined libraries (自訂資料庫, 2026-07-16): a source belongs to at
-- most one collection; NULL = 未分類. Deleting a collection releases its
-- sources back to 未分類 (ON DELETE SET NULL) — content is never removed.
CREATE TABLE IF NOT EXISTS collections (
    collection_id INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL                          -- UTC ISO-8601
);

CREATE TABLE IF NOT EXISTS api_usage (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,                        -- UTC ISO-8601
    provider      TEXT NOT NULL,                        -- groq / anthropic
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);

-- Hybrid retrieval (rule 5): FTS5 with trigram tokenizer — the default
-- tokenizer treats whole Chinese sentences as one token. External-content
-- table stays in sync with `chunks` via triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='chunk_id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.chunk_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.chunk_id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;
"""


class ModelMismatchError(Exception):
    """Configured embedding model differs from the one vectors were built with."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the mandatory pragmas and sqlite-vec loaded.

    check_same_thread=False: FastAPI runs a sync `yield` dependency and its
    endpoint in different threadpool threads. Each request/worker owns its
    own connection and never shares it concurrently, so relaxing the guard
    is safe; WAL + busy_timeout handle cross-connection concurrency.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    # WAL and FK enforcement are mandatory (rules 5 / 12-2); SQLite can
    # silently fall back, so verify instead of trusting the PRAGMA call.
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise RuntimeError(f"WAL mode required but got {mode!r} for {db_path}")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite build does not enforce foreign_keys")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Migration for databases created before display_title existed —
    # CREATE IF NOT EXISTS never alters an existing table.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    if "display_title" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN display_title TEXT")
    if "collection_id" not in cols:
        conn.execute(
            "ALTER TABLE sources ADD COLUMN collection_id INTEGER"
            " REFERENCES collections(collection_id) ON DELETE SET NULL"
        )
    # Retrieval trace (EVAL gap-3, 2026-07-19): the ranked chunks that fed an
    # assistant answer, with each chunk's two-path provenance (vector rank +
    # distance, FTS rank) so a wrong answer can be attributed to retrieval vs
    # generation. Assistant messages only; NULL on user rows and legacy rows.
    msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "retrieval_trace" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN retrieval_trace TEXT")
    # Backfill the FTS index for rows inserted before the triggers existed.
    # COUNT(*) on an external-content FTS table reads the content table, so
    # it cannot detect an empty index — use a meta flag instead; triggers
    # keep the index in sync once the one-time rebuild has run.
    if get_meta(conn, "fts_synced") != "1":
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
        set_meta(conn, "fts_synced", "1")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def ensure_embedding_model(conn: sqlite3.Connection, model: str, dim: int) -> None:
    """Startup gate (CLAUDE.md rule 14): refuse to run on model mismatch.

    First run records the model and creates the vec0 table sized to `dim`.
    """
    stored_model = get_meta(conn, "embedding_model")
    stored_dim = get_meta(conn, "embedding_dim")

    if stored_model is None:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors "
            f"USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{int(dim)}])"
        )
        set_meta(conn, "embedding_model", model)
        set_meta(conn, "embedding_dim", str(dim))
    elif stored_model != model or stored_dim != str(dim):
        raise ModelMismatchError(
            f"資料庫向量由 {stored_model}(dim={stored_dim})建立,"
            f"但目前設定為 {model}(dim={dim})。"
            "禁止混用:請改回原模型,或執行全庫向量重建後再啟動。"
        )
    # Kept-note vectors (Phase C) — separate from chunk_vectors by design
    # (rule 12). CREATE IF NOT EXISTS doubles as the migration for databases
    # that predate the notes feature.
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS note_vectors "
        f"USING vec0(note_id INTEGER PRIMARY KEY, embedding float[{int(dim)}])"
    )
    conn.commit()


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    """Cascade delete (CLAUDE.md rule 12-2): chunks + vectors + FTS rows.

    Chunks are deleted explicitly (not via FK cascade) so the FTS sync
    triggers reliably fire. Message citations are kept; readers mark them
    as deleted-source. Temp files/thumbnails are the caller's concern.
    """
    chunk_ids = [
        row["chunk_id"]
        for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE source_id = ?", (source_id,)
        )
    ]
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        conn.execute(
            f"DELETE FROM chunk_vectors WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )
    conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
    conn.commit()


def record_usage(
    conn: sqlite3.Connection,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    conn.execute(
        "INSERT INTO api_usage (ts, provider, model, input_tokens,"
        " output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
        (utcnow_iso(), provider, model, input_tokens, output_tokens, cost_usd),
    )
    conn.commit()


def month_cost_usd(conn: sqlite3.Connection) -> float:
    """Total recorded cost for the current UTC month."""
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE ts LIKE ?",
        (month_prefix + "%",),
    ).fetchone()
    return float(row[0])
