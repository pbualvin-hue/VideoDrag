"""Phase C distilled-notes store (PLAN-DISTILL.md).

Notes are AI output and live only in notes/note_vectors (rule 12);
the source RAG (chunks/chunk_vectors) never sees them, and search_knowledge
never mixes them in. Every note carries source_ids provenance. Since
2026-07-17 (盤點流程) notes are written as kept+embedded directly — writes
only happen in interactive conversations with the user consenting in-loop;
the legacy candidate branch remains only to drain pre-existing candidates.
"""

from __future__ import annotations

import json
import sqlite3

from .. import db
from ..textnorm import build_fts_query
from .embedder import Embedder, vector_to_blob

NOTE_KINDS = ("skill", "rule", "project", "money", "insight")
MAX_TITLE_CHARS = 100
MAX_CONTENT_CHARS = 2000
MAX_APPLICATION_CHARS = 1000
MAX_SOURCE_IDS = 20
# Spam bound: injected transcript content could mass-produce candidates; the
# queue is human-reviewed, so cap how much junk can pile up unreviewed.
MAX_PENDING_CANDIDATES = 100

TOP_K = 5
RRF_K = 60
MAX_VECTOR_DISTANCE = 1.0  # same calibration as chunk retrieval (bge-small-zh)


class NoteError(ValueError):
    """Invalid note input — message is user/model facing."""


def save_note(
    conn: sqlite3.Connection,
    kind: str,
    title: str,
    content: str,
    application: str,
    source_ids: list[int],
    embedder: "Embedder | None" = None,
) -> int:
    """Insert a note. Returns note_id.

    With `embedder` the note is written as KEPT and embedded immediately
    (盤點流程 2026-07-17:寫入只發生在使用者在場的互動討論中,候選層
    失去存在意義——原無頭週報已取消);without it, legacy candidate flow.
    Raises NoteError on invalid input or when the pending queue is full.
    """
    if kind not in NOTE_KINDS:
        raise NoteError(f"kind 必須是 {'/'.join(NOTE_KINDS)} 之一,收到 {kind!r}")
    title = (title or "").strip()[:MAX_TITLE_CHARS]
    content = (content or "").strip()[:MAX_CONTENT_CHARS]
    application = (application or "").strip()[:MAX_APPLICATION_CHARS]
    if not title or not content:
        raise NoteError("title 與 content 不可為空")
    # Both mandatory (review W2): application is the user's explicit ask
    # (適用情境/應用方式/build-now), and PLAN-DISTILL red line #2 requires
    # provenance — a note with no source isn't KB distillation and doesn't
    # belong in this store.
    if not application:
        raise NoteError("application 不可為空:必答適用情境/應用方式/build-now")
    try:
        ids = sorted({int(s) for s in (source_ids or [])})[:MAX_SOURCE_IDS]
    except (TypeError, ValueError) as exc:
        raise NoteError(f"source_ids 必須是整數陣列:{exc}") from exc
    if not ids:
        raise NoteError("source_ids 不可為空:筆記必附溯源(紅線 2)")
    known = {r["source_id"] for r in conn.execute(
        f"SELECT source_id FROM sources WHERE source_id IN"
        f" ({','.join('?' * len(ids))})", ids)}
    if not known:
        raise NoteError(f"source_ids {ids} 都不存在於庫內,溯源無效")

    if embedder is None:
        pending = conn.execute(
            "SELECT COUNT(*) FROM notes WHERE status = 'candidate'"
        ).fetchone()[0]
        if pending >= MAX_PENDING_CANDIDATES:
            raise NoteError(
                f"待確認筆記已達 {MAX_PENDING_CANDIDATES} 筆上限,"
                "請先到 PWA 管理頁「精煉筆記」處理"
            )
        cur = conn.execute(
            "INSERT INTO notes (kind, title, content, application, source_ids,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'candidate', ?)",
            (kind, title, content, application, json.dumps(ids),
             db.utcnow_iso()),
        )
        conn.commit()
        return cur.lastrowid

    # 直寫 kept 的節流(audit 🔵,與 delete 的 20/call 對稱):一小時 30 筆
    # 遠超正常盤點節奏,只擋注入誘導的大量灌入
    from datetime import datetime, timedelta, timezone
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute(
        "SELECT COUNT(*) FROM notes WHERE status = 'kept'"
        " AND created_at > ?", (hour_ago,),
    ).fetchone()[0]
    if recent >= 30:
        raise NoteError("一小時內已收錄 30 筆,先消化再繼續(防大量灌入)")

    now = db.utcnow_iso()
    cur = conn.execute(
        "INSERT INTO notes (kind, title, content, application, source_ids,"
        " status, created_at, decided_at) VALUES (?, ?, ?, ?, ?, 'kept', ?, ?)",
        (kind, title, content, application, json.dumps(ids), now, now),
    )
    note_id = cur.lastrowid
    vec = embedder.embed_query(f"{title}\n{content}")
    conn.execute(
        "INSERT OR REPLACE INTO note_vectors (note_id, embedding) VALUES (?, ?)",
        (note_id, vector_to_blob(vec)),
    )
    conn.commit()
    return note_id


def decide(
    conn: sqlite3.Connection,
    embedder: Embedder,
    note_id: int,
    keep: bool,
) -> str:
    """User verdict on a candidate: keep (embed, becomes retrievable) or drop
    (vector removed if any, row kept for audit). Returns the new status."""
    row = conn.execute(
        "SELECT note_id, title, content, status FROM notes WHERE note_id = ?",
        (note_id,),
    ).fetchone()
    if row is None:
        raise NoteError(f"找不到筆記 {note_id}")
    status = "kept" if keep else "dropped"
    if keep:
        vec = embedder.embed_query(f"{row['title']}\n{row['content']}")
        conn.execute(
            "INSERT OR REPLACE INTO note_vectors (note_id, embedding)"
            " VALUES (?, ?)",
            (note_id, vector_to_blob(vec)),
        )
    else:
        conn.execute("DELETE FROM note_vectors WHERE note_id = ?", (note_id,))
    conn.execute(
        "UPDATE notes SET status = ?, decided_at = ? WHERE note_id = ?",
        (status, db.utcnow_iso(), note_id),
    )
    conn.commit()
    return status


# Shared with chat retrieval so note search and source search tokenize
# identically (code review 2026-07-18).
_fts_query = build_fts_query


def search_kept(
    conn: sqlite3.Connection,
    embedder: Embedder,
    question: str,
    top_k: int = TOP_K,
) -> list[sqlite3.Row]:
    """Hybrid search over kept notes only (vector KNN + FTS5, RRF merge).

    Mirrors chunk retrieval but stays entirely inside the notes tables —
    the two stores never share a query path (rule 12).
    """
    qvec = vector_to_blob(embedder.embed_query(question))
    vec_rank = {
        r["note_id"]: rank
        for rank, r in enumerate(conn.execute(
            "SELECT note_id FROM note_vectors WHERE embedding MATCH ?"
            " AND k = ? AND distance <= ? ORDER BY distance",
            (qvec, top_k * 2, MAX_VECTOR_DISTANCE),
        ))
    }
    fts_rank: dict[int, int] = {}
    fts = _fts_query(question)
    if fts:
        try:
            # Filter to kept INSIDE the FTS arm (review W1): the FTS index
            # holds candidates/dropped too, and without this they eat the
            # rank budget and crowd kept notes out of exact-keyword recall.
            fts_rank = {
                r["note_id"]: rank
                for rank, r in enumerate(conn.execute(
                    "SELECT n.note_id FROM notes_fts f"
                    " JOIN notes n ON n.note_id = f.rowid"
                    " WHERE notes_fts MATCH ? AND n.status = 'kept'"
                    " ORDER BY rank LIMIT ?",
                    (fts, top_k * 2),
                ))
            }
        except sqlite3.OperationalError:
            fts_rank = {}

    scores: dict[int, float] = {}
    for nid, rank in vec_rank.items():
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (RRF_K + rank)
    for nid, rank in fts_rank.items():
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (RRF_K + rank)
    if not scores:
        return []

    placeholders = ",".join("?" * len(scores))
    rows = conn.execute(
        f"""SELECT note_id, kind, title, content, application, source_ids,
                   created_at
            FROM notes WHERE note_id IN ({placeholders})
            AND status = 'kept'""",
        list(scores),
    ).fetchall()
    ordered = sorted(rows, key=lambda r: scores[r["note_id"]], reverse=True)
    return ordered[:top_k]
