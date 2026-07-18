"""Download a full (audio+video) file for the vision path.

The audio-only ingest path can't produce frames, so when the judgment chain
calls for vision we fetch the smallest combined A/V file. Used both by the
inline fallback in the pipeline and the PWA "重新分析畫面" button.
"""

from __future__ import annotations

import logging
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .base import AdapterError, VideoTooLongError
from .normalize import NormalizedUrl

logger = logging.getLogger(__name__)

# yt-dlp works for both YouTube and TikTok; IG uses gallery-dl (Phase 4-5).
_YTDLP_PLATFORMS = {"youtube", "tiktok"}


def download_video(
    norm: NormalizedUrl, workdir: Path, max_duration_secs: int
) -> Path:
    if norm.platform not in _YTDLP_PLATFORMS:
        raise AdapterError(norm.platform, norm.canonical_url, "video_download",
                           f"vision not supported for platform {norm.platform}")
    # Probe duration first so the reanalyze path (which has no upstream audio
    # adapter to gate it) can't full-download a 3-hour video (rule 17).
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True,
                        "skip_download": True}) as ydl:
            info = ydl.extract_info(norm.canonical_url, download=False)
    except DownloadError as exc:
        raise AdapterError(norm.platform, norm.canonical_url, "probe",
                           str(exc)) from exc
    dur = int(info["duration"]) if info.get("duration") else None
    if dur and dur > max_duration_secs:
        raise VideoTooLongError(
            norm.platform, norm.canonical_url, "duration_check",
            f"duration {dur}s > limit {max_duration_secs}s",
        )
    workdir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(workdir / f"{norm.video_id}_full.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        # Smallest combined stream so frame extraction has actual pixels.
        "format": "worst[vcodec!=none]/worst",
        "outtmpl": outtmpl,
        "noplaylist": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(norm.canonical_url, download=True)
            path = Path(ydl.prepare_filename(info))
    except DownloadError as exc:
        raise AdapterError(norm.platform, norm.canonical_url, "video_download",
                           str(exc)) from exc
    if not path.exists():
        raise AdapterError(norm.platform, norm.canonical_url, "video_download",
                           f"file missing after download: {path}")
    return path
