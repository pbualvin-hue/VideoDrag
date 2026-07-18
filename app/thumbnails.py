"""Cover thumbnails for library cards (UX.md 分頁 2).

Stored as data/thumbnails/<source_id>.jpg. Cosmetic — download failures are
swallowed so they never affect ingest. The vision path writes its own cover
(a keyframe); this fills in a platform thumbnail for every other source.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

MAX_THUMB_BYTES = 2 * 1024 * 1024


def thumbnails_dir(settings: Settings) -> Path:
    d = settings.data_dir / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def thumbnail_path(settings: Settings, source_id: int) -> Path:
    return thumbnails_dir(settings) / f"{source_id}.jpg"


def has_thumbnail(settings: Settings, source_id: int) -> bool:
    return thumbnail_path(settings, source_id).exists()


def download_thumbnail(settings: Settings, source_id: int, url: str | None) -> bool:
    """Best-effort fetch of a platform thumbnail. Never raises."""
    if not url or has_thumbnail(settings, source_id):
        return False
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True)
        if resp.status_code != 200:
            return False
        data = resp.content[: MAX_THUMB_BYTES + 1]
        if len(data) > MAX_THUMB_BYTES:
            return False
        thumbnail_path(settings, source_id).write_bytes(data)
        return True
    except Exception as exc:  # noqa: BLE001 — cosmetic, never fatal
        logger.info("thumbnail fetch failed for source %s: %s", source_id, exc)
        return False
