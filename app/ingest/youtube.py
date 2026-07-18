"""YouTube adapter: official (manually created) captions first, then
yt-dlp audio-only download as fallback for Groq transcription.

Accuracy-first (CLAUDE.md rule 3): auto-generated captions are NOT trusted —
their quality for Chinese is well below whisper-large-v3, so only manually
created subtitles short-circuit transcription.

Politeness (CLAUDE.md rule 2): random delay before each network hit to
YouTube; never batch-hammer from the home IP.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from youtube_transcript_api import YouTubeTranscriptApi

from .base import (
    AdapterError,
    MediaResult,
    SourceMetadata,
    TranscriptSegment,
    VideoTooLongError,
)
from .normalize import NormalizedUrl

logger = logging.getLogger(__name__)

# Preferred caption languages, most specific first.
_CAPTION_LANGS = ("zh-TW", "zh-Hant", "zh-HK", "zh", "zh-Hans", "zh-CN", "en")


def _polite_delay() -> None:
    time.sleep(random.uniform(1.0, 3.5))


def _to_utc_iso(info: dict) -> str | None:
    """Best available publish time: epoch timestamp, else upload_date."""
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    upload_date = info.get("upload_date")  # YYYYMMDD, UTC per yt-dlp docs
    if upload_date:
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
    return None


def _probe_metadata(norm: NormalizedUrl) -> SourceMetadata:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(norm.canonical_url, download=False)
    except DownloadError as exc:
        raise AdapterError("youtube", norm.canonical_url, "metadata", str(exc)) from exc
    return SourceMetadata(
        platform="youtube",
        video_id=norm.video_id,
        title=info.get("title") or norm.video_id,
        url=norm.canonical_url,
        published_at=_to_utc_iso(info),
        duration_secs=int(info["duration"]) if info.get("duration") else None,
        thumbnail_url=info.get("thumbnail"),
    )


def _fetch_official_captions(video_id: str) -> list[TranscriptSegment] | None:
    """Return manually created captions, or None when only auto/none exist."""
    try:
        transcripts = YouTubeTranscriptApi().list(video_id)
    except Exception as exc:  # library raises many specific types; none are fatal
        logger.info("caption listing failed for %s: %s", video_id, exc)
        return None

    manual = [t for t in transcripts if not t.is_generated]
    if not manual:
        return None
    # Prefer Chinese variants, then English, then whatever exists.
    ranked = sorted(
        manual,
        key=lambda t: (
            _CAPTION_LANGS.index(t.language_code)
            if t.language_code in _CAPTION_LANGS
            else len(_CAPTION_LANGS)
        ),
    )
    try:
        fetched = ranked[0].fetch()
    except Exception as exc:
        logger.info("caption fetch failed for %s: %s", video_id, exc)
        return None
    return [
        TranscriptSegment(
            text=snippet.text.strip(),
            start_sec=snippet.start,
            end_sec=snippet.start + snippet.duration,
        )
        for snippet in fetched
        if snippet.text.strip()
    ]


def _download_audio(norm: NormalizedUrl, workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(workdir / f"{norm.video_id}.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        # Audio only, lowest quality: whisper-large-v3 is robust to low
        # bitrate and we downsample to 16kHz mono before upload anyway.
        # Fallback is "worst" (smallest combined stream), NOT bestaudio —
        # a video lacking audio-only formats must not trigger a huge download.
        "format": "worstaudio/worst",
        "outtmpl": outtmpl,
        "noplaylist": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(norm.canonical_url, download=True)
            path = Path(ydl.prepare_filename(info))
    except DownloadError as exc:
        raise AdapterError("youtube", norm.canonical_url, "audio_download", str(exc)) from exc
    if not path.exists():
        raise AdapterError(
            "youtube", norm.canonical_url, "audio_download",
            f"yt-dlp reported success but file missing: {path}",
        )
    return path


def fetch(
    norm: NormalizedUrl, workdir: Path, max_duration_secs: int
) -> MediaResult:
    """Adapter entry point: captions if官方字幕存在, else downloaded audio."""
    _polite_delay()
    metadata = _probe_metadata(norm)

    if metadata.duration_secs and metadata.duration_secs > max_duration_secs:
        raise VideoTooLongError(
            "youtube", norm.canonical_url, "duration_check",
            f"duration {metadata.duration_secs}s > limit {max_duration_secs}s",
        )

    captions = _fetch_official_captions(norm.video_id)
    if captions:
        logger.info("official captions found for %s (%d segments)",
                    norm.video_id, len(captions))
        return MediaResult(metadata=metadata, transcript=captions)

    _polite_delay()
    audio_path = _download_audio(norm, workdir)
    logger.info("audio downloaded for %s -> %s", norm.video_id, audio_path)
    return MediaResult(metadata=metadata, audio_path=audio_path)
