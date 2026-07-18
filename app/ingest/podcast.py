"""Podcast RSS adapter (rule 1 新型態 podcast, 2026-07-17 使用者拍板).

單集手動入庫:貼 RSS → 列集數 → 選集 → 下載 enclosure 音訊 → 走既有
Groq 轉錄管線。feed 與音訊都是任意公網 URL,沿用 article 的 SSRF 防護
(_stream_capped:公網限定、DNS pinning、串流位元組上限)。
"""

from __future__ import annotations

import email.utils
import hashlib
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .article import ArticleSSRFError, _stream_capped  # shared SSRF guard
from .base import AdapterError

logger = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BYTES = 300 * 1024 * 1024   # podcasts run large; hard stream cap
MAX_EPISODES = 20

_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@dataclass(frozen=True)
class Episode:
    guid: str
    title: str
    audio_url: str
    link: str | None            # episode web page, if the feed provides one
    published_at: str | None    # UTC ISO-8601
    duration_secs: int | None


def episode_id(ep_guid: str) -> str:
    """Stable dedup id (rule 11 analog): platform='podcast' + hash of guid."""
    return "ep_" + hashlib.sha256(ep_guid.encode("utf-8")).hexdigest()[:16]


def _parse_duration(raw: str | None) -> int | None:
    """itunes:duration comes as seconds or HH:MM:SS / MM:SS."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs


def _parse_pubdate(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    from datetime import timezone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_feed(url: str) -> dict:
    """Fetch + parse an RSS 2.0 podcast feed.

    Returns {"feed_title": str, "episodes": [Episode-dict, ...]} with the
    newest MAX_EPISODES episodes that carry an audio enclosure. Raises
    AdapterError with a plain-language reason on anything else (rule 18).
    """
    try:
        content = _stream_capped(url)[:MAX_FEED_BYTES]
    except ArticleSSRFError:
        raise
    except Exception as exc:  # noqa: BLE001 — wrapped with context below
        raise AdapterError("podcast", url, "fetch", str(exc)) from exc
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise AdapterError("podcast", url, "parse",
                           f"這個網址不是有效的 RSS(XML 解析失敗:{exc})") from exc
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise AdapterError("podcast", url, "parse",
                           "這個網址不是 Podcast RSS(找不到 rss/channel)")
    feed_title = (channel.findtext("title") or "").strip() or url

    episodes: list[dict] = []
    for item in channel.findall("item"):
        enc = item.find("enclosure")
        audio = enc.get("url") if enc is not None else None
        etype = (enc.get("type") or "") if enc is not None else ""
        if not audio or (etype and not etype.startswith("audio")):
            continue
        guid = (item.findtext("guid") or audio).strip()
        episodes.append({
            "guid": guid,
            "title": (item.findtext("title") or "(無標題)").strip(),
            "audio_url": audio,
            "link": (item.findtext("link") or "").strip() or None,
            "published_at": _parse_pubdate(item.findtext("pubDate")),
            "duration_secs": _parse_duration(
                item.findtext(f"{_ITUNES}duration")),
        })
        if len(episodes) >= MAX_EPISODES:
            break
    if not episodes:
        raise AdapterError("podcast", url, "parse",
                           "RSS 裡沒有任何帶音訊的集數")
    return {"feed_title": feed_title, "episodes": episodes}


def _probe_duration_secs(path: Path) -> int | None:
    """ffprobe duration; None when unprobeable (caller decides)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return int(float(proc.stdout.strip()))
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def download_episode(audio_url: str, workdir: Path,
                     max_duration_secs: int,
                     feed_duration: int | None) -> Path:
    """Download the enclosure (SSRF-guarded, byte-capped) into workdir.

    Duration is enforced twice (rule 17): from feed metadata before download
    when available, and via ffprobe afterwards as the source of truth.
    """
    if feed_duration and feed_duration > max_duration_secs:
        raise AdapterError("podcast", audio_url, "duration",
                           f"單集長度超過 {max_duration_secs // 3600} 小時上限")
    workdir.mkdir(parents=True, exist_ok=True)
    # Reuse the article SSRF-pinned streaming fetch, with the audio cap,
    # streamed STRAIGHT TO DISK (review W1: a 300MB episode buffered in RAM
    # would OOM the Pi). Enclosure URLs are arbitrary public hosts — exactly
    # the article threat model. Suffix is cosmetic; transcribe sniffs content.
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(audio_url.split("?")[0]).suffix) or ".mp3"
    path = workdir / f"episode{suffix}"
    _stream_capped(audio_url, max_bytes=MAX_AUDIO_BYTES, dest=path)
    probed = _probe_duration_secs(path)
    if probed and probed > max_duration_secs:
        path.unlink(missing_ok=True)
        raise AdapterError("podcast", audio_url, "duration",
                           f"單集實際長度超過 {max_duration_secs // 3600} 小時上限")
    if feed_duration is None and probed is None:
        # rule 17 的兩道時長檢查都不可用 → 只剩 300MB byte cap 兜底,
        # 明示於 log 不得靜默(review S3)
        logger.warning("episode duration unknown (no feed metadata, ffprobe "
                       "failed); byte cap only: %s", audio_url)
    return path
