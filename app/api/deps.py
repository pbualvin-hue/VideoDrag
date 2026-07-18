"""Shared API dependencies: per-request DB connection, settings, embedder."""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request

from .. import db
from ..config import Settings, load_settings
from ..rag.embedder import Embedder

_embedder_lock = threading.Lock()
_embedder: Embedder | None = None


def get_settings() -> Settings:
    # Re-reads os.environ so admin-page key updates apply without restart.
    return load_settings(require_keys=False)


def get_conn(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    from ..backup import restore_lock

    # Serialize the open against a restore file-swap (review C2); release
    # immediately — an in-flight connection keeps its inode and finishes safely.
    with restore_lock:
        conn = db.connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_embedder(
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> Embedder:
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            model = db.get_meta(conn, "embedding_model") or settings.embedding_model
            _embedder = Embedder(model)
        db.ensure_embedding_model(conn, _embedder.model_name, _embedder.dim)
        return _embedder


def require_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Second auth layer on top of Tailscale (CLAUDE.md 安全).

    While APP_TOKEN is unset the system is in first-run setup mode and
    requests are allowed (reachable only inside the tailnet by design).
    """
    if not settings.app_token:
        return
    supplied = (
        request.headers.get("x-app-token")
        or request.query_params.get("token")
        or ""
    )
    # Constant-time compare to avoid a token timing side-channel.
    if not secrets.compare_digest(supplied, settings.app_token):
        raise HTTPException(status_code=401, detail="無效或缺少 APP_TOKEN")
