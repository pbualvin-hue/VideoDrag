"""POST /api/chat, GET /api/sessions, GET /api/stats."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..config import Settings
from ..rag import chat
from ..rag.embedder import Embedder
from .deps import get_conn, get_embedder, get_settings, require_token

router = APIRouter(dependencies=[Depends(require_token)])


class ChatRequest(BaseModel):
    question: str
    session_id: str | None = None
    source_id: int | None = None
    collection_id: int | None = None   # 自訂資料庫範圍(2026-07-16)
    verify: bool = False


@router.get("/chat/starters")
def chat_starters(
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    """Empty-state opener questions. Cosmetic: a generation failure logs and
    returns none — it must never block the chat tab from rendering."""
    import logging
    try:
        return {"questions": chat.suggest_starters(conn, settings)}
    except Exception as exc:  # noqa: BLE001 — cosmetic, never fatal
        logging.getLogger(__name__).warning("starters generation failed: %s", exc)
        return {"questions": []}


@router.post("/chat")
def chat_endpoint(
    req: ChatRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
    embedder: Embedder = Depends(get_embedder),
):
    if not settings.anthropic_api_key:
        raise HTTPException(503, "尚未設定 ANTHROPIC_API_KEY,請先完成設定精靈")
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "問題不可為空")
    session_id = req.session_id or chat.new_session_id()
    result = chat.answer(
        conn, settings, embedder, question, session_id,
        source_id=req.source_id, collection_id=req.collection_id,
        verify=req.verify,
    )
    return {
        "session_id": result.session_id,
        "answer": result.answer,
        "citations": [
            {
                "label": label,
                "chunk_id": c.chunk_id,
                "source_id": c.source_id,
                "title": c.title,
                "display_title": c.display_title,
                "platform": c.platform,
                "url": c.url,
                "start_sec": c.start_sec,
                "end_sec": c.end_sec,
                "timestamp": chat.format_timestamp(c.start_sec),
                "published_at": c.published_at,
            }
            for label, c in result.cited_chunks
        ],
        "verification": result.verification,
        "budget_warning": result.budget_warning,
        "suggestions": result.suggestions,
        "trace": result.trace,
    }


@router.get("/sessions")
def sessions(conn: sqlite3.Connection = Depends(get_conn)):
    return {
        "sessions": [
            {"session_id": r["session_id"],
             "title": (r["first_question"] or "(無標題)")[:60],
             "last_at": r["last_at"], "message_count": r["n"]}
            for r in chat.list_sessions(conn)
        ]
    }


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """User-initiated removal of one conversation (回饋 2026-07-15).
    Messages only — sources/chunks are untouched (rule 12: chat history
    never feeds the RAG, so deleting it has no retrieval side effects)."""
    cur = conn.execute(
        "DELETE FROM messages WHERE session_id = ?", (session_id,)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "對話不存在")
    return {"deleted": session_id, "messages_removed": cur.rowcount}


@router.get("/sessions/{session_id}")
def session_messages(session_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    import json

    rows = conn.execute(
        "SELECT msg_id, role, content, cited_chunk_ids, retrieval_trace, created_at"
        " FROM messages WHERE session_id = ? ORDER BY msg_id",
        (session_id,),
    ).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        # Parse the stored trace back into a list so the client renders the
        # 依據 section on history reload (gap-3); tolerate legacy/NULL rows.
        raw = m.pop("retrieval_trace", None)
        try:
            m["trace"] = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            m["trace"] = []
        out.append(m)
    return {"messages": out}


@router.get("/stats")
def stats(
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    month = db.month_cost_usd(conn)
    breakdown = [dict(r) for r in conn.execute(
        "SELECT provider, model, COUNT(*) AS calls,"
        " SUM(input_tokens) AS input_tokens,"
        " SUM(output_tokens) AS output_tokens,"
        " ROUND(SUM(cost_usd), 4) AS cost_usd FROM api_usage"
        " WHERE ts LIKE strftime('%Y-%m', 'now') || '%'"
        " GROUP BY provider, model ORDER BY cost_usd DESC"
    )]
    return {
        "month_cost_usd": round(month, 4),
        "budget_usd": settings.monthly_budget_usd,
        "over_budget": month > settings.monthly_budget_usd,
        "breakdown": breakdown,
    }
