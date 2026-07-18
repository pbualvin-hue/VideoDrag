"""/api/collections — user-defined libraries (自訂資料庫, 2026-07-16).

A collection is a label a source can belong to (at most one). Deleting a
collection releases its sources back to 未分類; content is never removed.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from .deps import get_conn, require_token

router = APIRouter(dependencies=[Depends(require_token)])

MAX_NAME_CHARS = 30
MAX_COLLECTIONS = 50


class CollectionRequest(BaseModel):
    name: str


def _clean_name(raw: str) -> str:
    name = " ".join(raw.split())  # collapse inner whitespace, strip ends
    if not name:
        raise HTTPException(400, "名稱不可為空")
    if len(name) > MAX_NAME_CHARS:
        raise HTTPException(400, f"名稱超過 {MAX_NAME_CHARS} 字上限")
    return name


@router.get("/collections")
def list_collections(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute(
        """SELECT c.collection_id, c.name,
                  (SELECT COUNT(*) FROM sources s
                   WHERE s.collection_id = c.collection_id) AS source_count
           FROM collections c ORDER BY c.name"""
    ).fetchall()
    unfiled = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE collection_id IS NULL"
    ).fetchone()[0]
    return {"collections": [dict(r) for r in rows], "unfiled_count": unfiled}


@router.post("/collections")
def create_collection(req: CollectionRequest,
                      conn: sqlite3.Connection = Depends(get_conn)):
    name = _clean_name(req.name)
    total = conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
    if total >= MAX_COLLECTIONS:
        raise HTTPException(400, f"資料庫數已達 {MAX_COLLECTIONS} 上限")
    try:
        cur = conn.execute(
            "INSERT INTO collections (name, created_at) VALUES (?, ?)",
            (name, db.utcnow_iso()),
        )
    except sqlite3.IntegrityError as exc:
        # Only the name UNIQUE can trip here today; keep unexpected
        # integrity errors loud instead of mislabeled as duplicates (S3).
        if "UNIQUE" in str(exc):
            raise HTTPException(409, f"已有同名資料庫「{name}」")
        raise
    conn.commit()
    return {"collection_id": cur.lastrowid, "name": name}


@router.patch("/collections/{collection_id}")
def rename_collection(collection_id: int, req: CollectionRequest,
                      conn: sqlite3.Connection = Depends(get_conn)):
    name = _clean_name(req.name)
    try:
        cur = conn.execute(
            "UPDATE collections SET name = ? WHERE collection_id = ?",
            (name, collection_id),
        )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, f"已有同名資料庫「{name}」")
        raise
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "資料庫不存在")
    return {"collection_id": collection_id, "name": name}


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: int,
                      conn: sqlite3.Connection = Depends(get_conn)):
    """Sources fall back to 未分類 (FK ON DELETE SET NULL) — never deleted."""
    released = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE collection_id = ?",
        (collection_id,),
    ).fetchone()[0]
    cur = conn.execute(
        "DELETE FROM collections WHERE collection_id = ?", (collection_id,)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "資料庫不存在")
    return {"deleted": collection_id, "sources_released": released}


class AssignRequest(BaseModel):
    collection_id: int | None = None  # null = 移回未分類


@router.patch("/videos/{source_id}/collection")
def assign_collection(source_id: int, req: AssignRequest,
                      conn: sqlite3.Connection = Depends(get_conn)):
    if req.collection_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM collections WHERE collection_id = ?",
            (req.collection_id,),
        ).fetchone()
        if not exists:
            raise HTTPException(404, "資料庫不存在")
    cur = conn.execute(
        "UPDATE sources SET collection_id = ? WHERE source_id = ?",
        (req.collection_id, source_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "來源不存在")
    return {"source_id": source_id, "collection_id": req.collection_id}
