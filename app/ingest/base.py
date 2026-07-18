"""Shared adapter interface (CLAUDE.md 平台 adapter 隔離原則).

Every platform adapter implements: fetch(url) -> MediaResult carrying either
a ready transcript (official captions) or a downloaded audio path.
yt-dlp usage must stay inside adapters; failures raise AdapterError with
platform / URL / stage / original error — no silent cross-platform fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class AdapterError(Exception):
    """Explicit adapter failure with full context for the jobs.error column."""

    def __init__(self, platform: str, url: str, stage: str, original: str):
        self.platform = platform
        self.url = url
        self.stage = stage
        self.original = original
        super().__init__(f"[{platform}] {stage} failed for {url}: {original}")


class VideoTooLongError(AdapterError):
    """Video exceeds MAX_VIDEO_DURATION_SECS (CLAUDE.md rule 17): reject early."""


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class SourceMetadata:
    platform: str
    video_id: str
    title: str
    url: str                      # canonical URL
    published_at: str | None      # UTC ISO-8601
    duration_secs: int | None
    type: str = "video"
    thumbnail_url: str | None = None  # platform cover image, for library cards


@dataclass(frozen=True)
class MediaResult:
    metadata: SourceMetadata
    # Exactly one of the two payloads is set:
    transcript: list[TranscriptSegment] = field(default_factory=list)
    audio_path: Path | None = None

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript)
