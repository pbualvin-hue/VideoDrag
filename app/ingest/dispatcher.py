"""Route (platform, video_id) to the matching adapter.

Adapters never fall back to another platform silently (CLAUDE.md
adapter isolation): unsupported platforms raise an explicit error.
"""

from __future__ import annotations

from pathlib import Path

from . import instagram, tiktok, youtube
from .base import AdapterError, MediaResult
from .normalize import NormalizedUrl

_ADAPTERS = {
    "youtube": youtube.fetch,
    "tiktok": tiktok.fetch,
    "instagram": instagram.fetch,  # needs data/ig_cookies.txt uploaded
    # "web": handled in pipeline
}


def fetch(norm: NormalizedUrl, workdir: Path, max_duration_secs: int) -> MediaResult:
    adapter = _ADAPTERS.get(norm.platform)
    if adapter is None:
        raise AdapterError(
            norm.platform, norm.canonical_url, "dispatch",
            f"platform {norm.platform!r} not supported yet (Phase 4)",
        )
    return adapter(norm, workdir, max_duration_secs)
