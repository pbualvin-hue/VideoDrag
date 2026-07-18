"""URL expansion and normalization.

Security surface (CLAUDE.md 安全/rule 11): the video ingest path accepts
ONLY whitelisted platform domains. Short-link expansion follows at most
3 redirect hops, every hop must stay on the whitelist, http(s) only.
Parsing is pure; only `expand_url` touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlparse

import httpx

MAX_REDIRECT_HOPS = 3

# Whitelisted hosts per platform, including official short-link domains.
# Matching accepts the domain itself and any subdomain.
_PLATFORM_DOMAINS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
}
_ALL_DOMAINS = tuple(d for ds in _PLATFORM_DOMAINS.values() for d in ds)

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TIKTOK_ID_RE = re.compile(r"^\d+$")
_INSTAGRAM_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")


class NormalizeError(Exception):
    """URL is not an accepted, parseable platform video URL."""


@dataclass(frozen=True)
class NormalizedUrl:
    platform: str
    video_id: str
    canonical_url: str


def _host_of(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NormalizeError(f"僅接受 http(s) 連結:{url}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise NormalizeError(f"無法解析主機名稱:{url}")
    return host


def _domain_allowed(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _ALL_DOMAINS)


def _platform_of(host: str) -> str | None:
    for platform, domains in _PLATFORM_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    return None


def parse_video_url(url: str) -> NormalizedUrl:
    """Pure parse: platform URL -> (platform, video_id, canonical URL).

    Strips tracking params by reconstructing the canonical form.
    Raises NormalizeError when the URL is off-whitelist or unrecognized.
    """
    host = _host_of(url)
    if not _domain_allowed(host):
        raise NormalizeError(f"網域不在支援平台白名單內:{host}")

    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    platform = _platform_of(host)

    if platform == "youtube":
        video_id = None
        if host == "youtu.be" and path_parts:
            video_id = path_parts[0]
        elif path_parts and path_parts[0] in ("shorts", "live", "embed", "v"):
            video_id = path_parts[1] if len(path_parts) > 1 else None
        elif parsed.path == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [None])[0]
        if not video_id or not _YOUTUBE_ID_RE.match(video_id):
            raise NormalizeError(f"無法從 YouTube 連結取出影片 ID:{url}")
        return NormalizedUrl(
            "youtube", video_id, f"https://www.youtube.com/watch?v={video_id}"
        )

    if platform == "tiktok":
        # https://www.tiktok.com/@user/video/1234567890
        if len(path_parts) >= 3 and path_parts[1] in ("video", "photo"):
            video_id = path_parts[2]
            if _TIKTOK_ID_RE.match(video_id):
                return NormalizedUrl(
                    "tiktok",
                    video_id,
                    f"https://www.tiktok.com/{path_parts[0]}/video/{video_id}",
                )
        raise NormalizeError(f"無法從 TikTok 連結取出影片 ID(短連結需先展開):{url}")

    if platform == "instagram":
        # https://www.instagram.com/reel/CODE/  |  /reels/CODE  |  /p/CODE
        if len(path_parts) >= 2 and path_parts[0] in ("reel", "reels", "p", "tv"):
            code = path_parts[1]
            if _INSTAGRAM_CODE_RE.match(code):
                return NormalizedUrl(
                    "instagram", code, f"https://www.instagram.com/reel/{code}/"
                )
        raise NormalizeError(f"無法從 Instagram 連結取出內容代碼:{url}")

    raise NormalizeError(f"不支援的平台:{host}")


def _default_head(url: str) -> tuple[int, str | None]:
    """One HEAD request without auto-redirects; returns (status, location)."""
    resp = httpx.head(url, follow_redirects=False, timeout=10.0)
    return resp.status_code, resp.headers.get("location")


def expand_url(
    url: str, head: Callable[[str], tuple[int, str | None]] = _default_head
) -> str:
    """Follow short-link redirects, max 3 hops, whitelist enforced per hop."""
    current = url
    hops = 0
    while True:
        host = _host_of(current)
        if not _domain_allowed(host):
            raise NormalizeError(f"轉址落到白名單外的網域,拒絕跟隨:{host}")
        status, location = head(current)
        if status not in (301, 302, 303, 307, 308) or not location:
            return current
        hops += 1
        if hops > MAX_REDIRECT_HOPS:
            raise NormalizeError(f"轉址超過 {MAX_REDIRECT_HOPS} 層上限,拒絕跟隨:{url}")
        # Resolve relative Location against the current URL.
        current = str(httpx.URL(current).join(location))


def resolve(url: str) -> NormalizedUrl:
    """Parse directly when possible; expand short links only when needed."""
    try:
        return parse_video_url(url)
    except NormalizeError:
        host = _host_of(url)
        if not _domain_allowed(host):
            raise
    return parse_video_url(expand_url(url))


def classify(url: str) -> NormalizedUrl:
    """Route a shared URL to a video platform or the article path.

    A URL on a video-platform domain (incl. short links) is resolved as
    video; anything else on a public http(s) host is treated as an article
    (platform='web'). The article path enforces its own SSRF guard at fetch
    time, so no domain whitelist applies here — but scheme is still checked.
    """
    host = _host_of(url)  # raises on non-http(s)
    if _domain_allowed(host):
        return resolve(url)
    p = urlparse(url)
    article_id = f"{host}{p.path}".rstrip("/")
    return NormalizedUrl("web", article_id, url)
