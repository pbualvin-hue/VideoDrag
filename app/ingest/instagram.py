"""Instagram Reels adapter (CLAUDE.md rule 2): gallery-dl primary, yt-dlp
fallback. IG requires login for almost all content, so this needs the user's
Netscape cookies.txt uploaded via the admin page (data/ig_cookies.txt).

gallery-dl handles IG login + metadata and delegates the actual video fetch to
yt-dlp internally; it writes the reel .mp4 (flat in workdir) plus an info.json
of post metadata. The returned .mp4 is used as audio_path — the pipeline's
preprocess_audio (ffmpeg -vn) extracts the audio stream regardless of
container. Requests are throttled (rule 2: 對平台保持禮貌節流).
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ..config import DATA_DIR
from .base import AdapterError, MediaResult, SourceMetadata, VideoTooLongError
from .normalize import NormalizedUrl

logger = logging.getLogger(__name__)

_COOKIES = DATA_DIR / "ig_cookies.txt"


def _published_at(raw: str | None) -> str | None:
    if not raw:
        return None
    try:  # gallery-dl gives a naive UTC "YYYY-MM-DD HH:MM:SS"
        return (
            datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except ValueError:
        return None


def _safe_int(value) -> int | None:
    # Metadata from gallery-dl/yt-dlp is semi-trusted; a non-numeric duration
    # must not escape as a bare ValueError (adapters always raise AdapterError).
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _check_duration(duration: int | None, norm: NormalizedUrl, limit: int) -> None:
    if duration and duration > limit:
        raise VideoTooLongError(
            "instagram", norm.canonical_url, "duration_check",
            f"duration {duration}s > limit {limit}s",
        )


_IMAGE_GLOBS = ("*.jpg", "*.jpeg", "*.png", "*.webp")


def _load_meta(workdir: Path) -> dict:
    """Read gallery-dl post metadata. A single post writes `info.json`; a
    carousel may write per-item `*.json` sidecars — take the first that parses
    (caption/date are per-post, identical across items)."""
    candidates = [workdir / "info.json", *sorted(workdir.glob("*.json"))]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _via_gallery_dl(norm: NormalizedUrl, workdir: Path, limit: int) -> MediaResult:
    proc = subprocess.run(
        ["gallery-dl", "--cookies", str(_COOKIES), "-D", str(workdir),
         "--write-info-json", "--no-mtime", "--sleep-request", "3.0",
         norm.canonical_url],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise AdapterError("instagram", norm.canonical_url, "gallery-dl",
                           (proc.stderr or "").strip()[-400:] or "gallery-dl 失敗")

    meta = _load_meta(workdir)
    caption = (meta.get("description") or "").strip()
    published = _published_at(meta.get("post_date") or meta.get("date"))

    mp4s = list(workdir.glob("*.mp4"))
    if mp4s:
        duration = _safe_int(meta.get("audio_duration"))
        _check_duration(duration, norm, limit)
        title = caption or (meta.get("username") or "").strip() or norm.video_id
        return MediaResult(
            metadata=SourceMetadata(
                platform="instagram", video_id=norm.video_id, title=title,
                url=norm.canonical_url, published_at=published,
                duration_secs=duration, thumbnail_url=None),
            audio_path=mp4s[0],
        )

    # No video: an image / carousel post (rule 1: type="image"). gallery-dl
    # already fetched the images; the pipeline reads them via the vision path
    # and keeps the caption as article text.
    images = sorted(p for g in _IMAGE_GLOBS for p in workdir.glob(g))
    if not images:
        raise AdapterError("instagram", norm.canonical_url, "gallery-dl",
                           "未產出影片或圖片(可能 cookie 失效或內容已下架)")
    title = _first_line(caption) or (meta.get("username") or "").strip() or norm.video_id
    return MediaResult(
        metadata=SourceMetadata(
            platform="instagram", video_id=norm.video_id, title=title,
            url=norm.canonical_url, published_at=published,
            duration_secs=None, type="image", thumbnail_url=None),
        image_paths=images, caption=caption or None,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:120]
    return ""


def _via_ytdlp(norm: NormalizedUrl, workdir: Path, limit: int) -> MediaResult:
    outtmpl = str(workdir / f"{norm.video_id}.%(ext)s")
    opts = {
        "quiet": True, "no_warnings": True, "format": "worst",
        "outtmpl": outtmpl, "noplaylist": True, "cookiefile": str(_COOKIES),
        "socket_timeout": 60,  # bound a stalled fallback (parity with gallery-dl timeout)
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(norm.canonical_url, download=True)
        path = Path(ydl.prepare_filename(info))
    duration = _safe_int(info.get("duration"))
    _check_duration(duration, norm, limit)
    if not path.exists():
        raise AdapterError("instagram", norm.canonical_url, "yt-dlp",
                           f"file missing after download: {path}")
    published = None
    if info.get("timestamp"):
        published = datetime.fromtimestamp(
            info["timestamp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = (info.get("description") or info.get("title") or "").strip() or norm.video_id
    return MediaResult(
        metadata=SourceMetadata(
            platform="instagram", video_id=norm.video_id, title=title,
            url=norm.canonical_url, published_at=published,
            duration_secs=duration, thumbnail_url=info.get("thumbnail")),
        audio_path=path,
    )


def fetch(norm: NormalizedUrl, workdir: Path, max_duration_secs: int) -> MediaResult:
    if not _COOKIES.is_file():
        raise AdapterError("instagram", norm.canonical_url, "cookie",
                           "尚未上傳 Instagram cookie(管理頁上傳後再試)")
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        return _via_gallery_dl(norm, workdir, max_duration_secs)
    except VideoTooLongError:
        raise  # too long is definitive — don't retry with the fallback
    except (AdapterError, subprocess.TimeoutExpired) as primary:
        logger.warning("gallery-dl failed for %s; trying yt-dlp fallback", norm.video_id)
        try:
            return _via_ytdlp(norm, workdir, max_duration_secs)
        except VideoTooLongError:
            raise
        except (AdapterError, DownloadError, subprocess.TimeoutExpired) as fb:
            raise AdapterError(
                "instagram", norm.canonical_url, "download",
                f"gallery-dl 與 yt-dlp 皆失敗: {primary} || {fb}",
            ) from fb
