"""TikTok adapter (CLAUDE.md rule 2): yt-dlp for audio.

Short links (vt.tiktok.com / vm.tiktok.com) are expanded and whitelist-
checked upstream in normalize.resolve before reaching this adapter, so
`norm` here always carries a canonical /video/<id> URL. TikTok posts rarely
have official captions, so this always goes through audio -> Groq.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .base import (
    AdapterError,
    MediaResult,
    SourceMetadata,
    VideoTooLongError,
)
from .normalize import NormalizedUrl

logger = logging.getLogger(__name__)


def _to_utc_iso(info: dict) -> str | None:
    ts = info.get("timestamp")
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    upload_date = info.get("upload_date")  # YYYYMMDD
    if upload_date:
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
    return None


def _title_of(info: dict, video_id: str) -> str:
    # TikTok has no real title; fall back to the caption/description.
    return (info.get("title") or info.get("description") or "").strip() or video_id


def fetch(norm: NormalizedUrl, workdir: Path, max_duration_secs: int) -> MediaResult:
    workdir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(workdir / f"{norm.video_id}.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "worstaudio/worst",
        "outtmpl": outtmpl,
        "noplaylist": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(norm.canonical_url, download=True)
            path = Path(ydl.prepare_filename(info))
    except DownloadError as exc:
        raise AdapterError("tiktok", norm.canonical_url, "audio_download",
                           str(exc)) from exc

    duration = int(info["duration"]) if info.get("duration") else None
    if duration and duration > max_duration_secs:
        raise VideoTooLongError(
            "tiktok", norm.canonical_url, "duration_check",
            f"duration {duration}s > limit {max_duration_secs}s",
        )
    if not path.exists():
        raise AdapterError("tiktok", norm.canonical_url, "audio_download",
                           f"file missing after download: {path}")

    metadata = SourceMetadata(
        platform="tiktok", video_id=norm.video_id,
        title=_title_of(info, norm.video_id), url=norm.canonical_url,
        published_at=_to_utc_iso(info), duration_secs=duration,
        thumbnail_url=info.get("thumbnail"),
    )
    return MediaResult(metadata=metadata, audio_path=path)
