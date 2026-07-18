"""/api/notes — Phase C note approval for the PWA (PLAN-DISTILL.md).

The human decision lives here: candidates arrive via the MCP save_note tool,
and only this router (APP_TOKEN-protected) can promote them to `kept`
(embedded, retrievable) or `dropped`.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..rag import notes
from ..rag.embedder import Embedder
from .deps import get_conn, get_embedder, require_token

router = APIRouter(dependencies=[Depends(require_token)])


def resolve_note_sources(conn: sqlite3.Connection,
                         source_ids_json: str | None) -> list[dict]:
    """溯源 id 轉成可讀標題;來源已刪除時 title=None(小修 O3:
    筆記存活期長於來源,裸 id 對使用者無意義且刪除後成懸空)。"""
    try:
        ids = json.loads(source_ids_json or "[]")
    except (ValueError, TypeError):
        ids = []
    out = []
    for sid in ids[:8]:
        row = conn.execute(
            "SELECT display_title, title FROM sources WHERE source_id = ?",
            (sid,),
        ).fetchone()
        out.append({"source_id": sid,
                    "title": (row["display_title"] or row["title"]) if row else None})
    return out


@router.get("/notes")
def list_notes(
    status: str = "candidate",
    conn: sqlite3.Connection = Depends(get_conn),
):
    if status not in ("candidate", "kept", "dropped"):
        raise HTTPException(400, "status 必須是 candidate/kept/dropped")
    rows = conn.execute(
        "SELECT note_id, kind, title, content, application, source_ids,"
        " status, created_at, decided_at FROM notes WHERE status = ?"
        " ORDER BY note_id DESC LIMIT 100",
        (status,),
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(*) FROM notes WHERE status = 'candidate'"
    ).fetchone()[0]
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = resolve_note_sources(conn, r["source_ids"])
        out.append(d)
    return {"notes": out, "pending_count": pending}


class DecideRequest(BaseModel):
    keep: bool


@router.post("/notes/{note_id}/decide")
def decide_note(
    note_id: int,
    req: DecideRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    embedder: Embedder = Depends(get_embedder),
):
    try:
        status = notes.decide(conn, embedder, note_id, keep=req.keep)
    except notes.NoteError as exc:
        raise HTTPException(404, str(exc))
    return {"note_id": note_id, "status": status}
