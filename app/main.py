"""FastAPI entry point: API + PWA static shell + in-process worker/backup.

Run: uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from . import db
from .api import (routes_admin, routes_chat, routes_collections,
                  routes_ingest, routes_mcp, routes_notes)
from .backup import BackupScheduler
from .config import load_settings
from .worker import Worker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class NoCacheStatic(StaticFiles):
    """Serve the PWA shell with `Cache-Control: no-cache` so every load
    revalidates against the ETag. Without it, Starlette sends only ETag +
    Last-Modified; browsers then apply *heuristic* caching and keep serving a
    stale app.js after a deploy — which silently drops new API fields the old
    JS never reads (the gap-3 「這則回答的依據」 panel was invisible for exactly
    this reason). Revalidation is cheap: unchanged assets return 304."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings(require_keys=False)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    conn.close()
    app.state.worker = Worker(settings)
    app.state.worker.start()
    app.state.backup_scheduler = BackupScheduler(settings)
    app.state.backup_scheduler.start()
    yield
    app.state.worker.stop()
    app.state.backup_scheduler.stop()


app = FastAPI(title="vidrag", lifespan=lifespan)
app.include_router(routes_admin.setup_router, prefix="/api")
app.include_router(routes_ingest.router, prefix="/api")
app.include_router(routes_chat.router, prefix="/api")
app.include_router(routes_admin.router, prefix="/api")
app.include_router(routes_mcp.router, prefix="/api")
app.include_router(routes_notes.router, prefix="/api")
app.include_router(routes_collections.router, prefix="/api")
app.mount("/", NoCacheStatic(directory=WEB_DIR, html=True), name="web")
