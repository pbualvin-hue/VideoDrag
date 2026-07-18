"""Web article adapter (CLAUDE.md 安全 / rule 2).

Unlike the video path (platform whitelist), articles may come from any
PUBLIC domain — so this path carries its own SSRF guard:
- every resolved IP (across redirects) must be a global-scope, non-blocked
  address; an explicit private/reserved blocklist backs up ipaddress
  (CVE-2024-4032 mislabelled ranges on older CPython);
- the connection is PINNED to the validated IP so a rebinding attacker
  can't swap in a private address between our check and httpx's own DNS
  resolution (a real TOCTOU otherwise);
- GET only, streamed with a hard 5MB cap (bounds memory + decompression
  bombs, unlike buffering the whole body then slicing).
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import re
import socket
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

from pathlib import Path

import httpx
import trafilatura

from .base import AdapterError, MediaResult, SourceMetadata, TranscriptSegment

logger = logging.getLogger(__name__)

MAX_ARTICLE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
_TIMEOUT = 15.0

# A login wall / JS-only page still yields a non-empty extraction (e.g. Google
# Drive gives "載入中…登入"), which would otherwise be stored as a `ready`
# source and pollute the RAG with an empty shell. Reject it so the job is
# marked failed and stays retryable (pipeline re-ingests failed sources).
MIN_ARTICLE_CHARS = 200
# Rendered-by-real-browser floor (jina path): a survey/product page's whole
# visible pitch can be legitimately short (Typeform case, 2026-07-15).
MIN_RENDERED_CHARS = 60
_WALL_MAX_CHARS = 500  # markers only indict a page that is ALSO suspiciously short

# Latin markers need word boundaries: an unanchored "log in" also matches
# "blog in", and "loading" matches "downloading" — both are ordinary prose.
_WALL_RE_LATIN = re.compile(
    r"\b(sign in|log in|create an account|enable javascript|"
    r"javascript is disabled|loading)\b"
)
# CJK has no word boundaries, so these stay substring matches — against
# whitespace-stripped text, since a wall's nav items arrive newline-separated.
# Simplified variants are mandatory: OpenCC s2twp runs AFTER the adapter, so
# 知乎/CSDN-style walls reach this guard still in simplified script.
_WALL_MARKERS_CJK = (
    "載入中", "载入中", "登入", "登录", "登録", "請登錄", "请登录",
    "訂閱以繼續", "订阅以继续", "會員專屬", "会员专属",
    "開啟javascript", "开启javascript", "繼續瀏覽", "继续浏览",
)
# A realistic browser UA — many sites 403 generic/bot agents.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Explicit blocklist backing up ipaddress.is_global (CVE-2024-4032: older
# CPython mislabelled several ranges). Includes cloud metadata (169.254/16).
_BLOCKED_NETS = [
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
        "198.18.0.0/15", "::1/128", "fc00::/7", "fe80::/10", "::ffff:0:0/96",
    )
]


class ArticleSSRFError(AdapterError):
    """Article URL resolves to a non-public address or violates fetch policy."""


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    if not ip.is_global or ip.is_multicast:
        return True
    # IPv4-mapped IPv6 (::ffff:a.b.c.d) — evaluate the embedded v4 too.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        if _ip_blocked(ip.ipv4_mapped):
            return True
    return any(ip in net for net in _BLOCKED_NETS)


def _validated_ip(url: str) -> tuple[str, str, int]:
    """Return (host, pinned_ip, port) after asserting the host resolves only
    to public addresses. Raises on non-http(s) or any private/reserved IP."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ArticleSSRFError("web", url, "scheme", "僅接受 http(s)")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ArticleSSRFError("web", url, "host", "無法解析主機名稱")
    try:
        infos = socket.getaddrinfo(host, parsed.port or
                                   (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ArticleSSRFError("web", host, "dns", str(exc)) from exc
    pinned: str | None = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if _ip_blocked(ip):
            raise ArticleSSRFError(
                "web", url, "ssrf",
                f"主機 {host} 解析到非公網位址 {ip},拒絕抓取",
            )
        pinned = pinned or info[4][0]
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, pinned, port


_resolve_lock = threading.Lock()


@contextlib.contextmanager
def _pin_dns(host: str, ip: str, port: int):
    """Force getaddrinfo to return the pre-validated IP for `host` only.

    Keeping the hostname in the URL means TLS SNI + cert verification work
    normally, while the socket connects to the exact IP we checked — so a
    rebinding attacker can't swap in a private address between check and
    connect. Serialized by a lock — necessary now that /podcast/feed also
    calls this from FastAPI's request threadpool, not just the worker."""
    original = socket.getaddrinfo
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def patched(h, p, *args, **kwargs):
        if h == host:
            return [(family, socket.SOCK_STREAM, 6, "", (ip, p or port))]
        return original(h, p, *args, **kwargs)

    with _resolve_lock:
        socket.getaddrinfo = patched
        try:
            yield
        finally:
            socket.getaddrinfo = original


def _stream_capped(url: str, max_bytes: int = MAX_ARTICLE_BYTES,
                   dest: "Path | None" = None) -> bytes:
    """GET with manual redirect handling; every hop is re-validated + DNS-
    pinned, body streamed with a hard byte cap (iter_bytes yields DECOMPRESSED
    bytes, so this also bounds decompression bombs). `max_bytes` lets the
    podcast adapter reuse the same guard with an audio-sized cap; passing
    `dest` streams chunks straight to that file instead of RAM — a 300MB
    episode must never be buffered in memory on the Pi (review W1)."""
    current = url
    headers = {"User-Agent": _UA, "Accept-Language": "zh-TW,zh,en;q=0.8"}
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=False,
                      verify=True) as client:
        for _ in range(MAX_REDIRECTS + 1):
            host, ip, port = _validated_ip(current)  # raises on private IP
            with _pin_dns(host, ip, port):
                with client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise ArticleSSRFError(
                                "web", current, "fetch",
                                f"HTTP {resp.status_code} 無 Location")
                        current = str(httpx.URL(current).join(location))
                        continue
                    if resp.status_code != 200:
                        raise AdapterError("web", current, "fetch",
                                           f"HTTP {resp.status_code}")
                    total = 0
                    if dest is not None:
                        try:
                            with dest.open("wb") as fh:
                                for chunk in resp.iter_bytes():
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise AdapterError(
                                            "web", current, "fetch",
                                            f"內容超過 {max_bytes // 2**20}MB 上限")
                                    fh.write(chunk)
                        except Exception:
                            dest.unlink(missing_ok=True)
                            raise
                        return b""
                    parts: list[bytes] = []
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise AdapterError(
                                "web", current, "fetch",
                                f"內容超過 {max_bytes // 2**20}MB 上限")
                        parts.append(chunk)
                    return b"".join(parts)
    raise ArticleSSRFError("web", url, "redirect", "轉址超過上限")


def _wall_reason(text: str, min_chars: int = MIN_ARTICLE_CHARS) -> str | None:
    """Why `text` is not a usable article body, or None if it looks real.

    Length gates first; markers only indict text that is ALSO short, so a long
    article merely discussing "登入" or "sign in" passes. Counts non-whitespace
    characters: a wall's nav items arrive newline-padded and would otherwise
    pad their way past the threshold. The cloud-render path passes a lower
    `min_chars`: its text came out of a real browser, so short usually means
    「這頁可見內容就這麼多」(e.g. a survey pitch), not an empty shell.
    """
    compact = re.sub(r"\s+", "", text)
    n = len(compact)
    if n == 0:
        return "無法從此網頁擷取正文內容"
    if n < min_chars:
        return f"正文僅 {n} 字,疑似登入牆或需 JavaScript 動態載入的頁面"
    if n >= _WALL_MAX_CHARS:
        return None
    hit = next((m for m in _WALL_MARKERS_CJK if m in compact.lower()), None)
    if hit is None:
        match = _WALL_RE_LATIN.search(text.lower())
        hit = match.group(1) if match else None
    if hit:
        return f"正文過短({n} 字)且含樣板字串「{hit}」,疑似登入牆"
    return None


def fetch(url: str) -> MediaResult:
    try:
        content = _stream_capped(url)
    except httpx.HTTPError as exc:
        raise AdapterError("web", url, "fetch", str(exc)) from exc

    # Parse once from raw bytes so trafilatura sniffs the real charset (a
    # Big5/GBK page served without a charset header would be mis-decoded).
    tree = trafilatura.load_html(content)
    if tree is None:
        raise AdapterError("web", url, "extract", "無法解析此網頁")
    extracted = trafilatura.extract(
        tree, output_format="txt", include_comments=False,
        include_tables=True, url=url,
    )
    text = (extracted or "").strip()
    reason = _wall_reason(text)
    if reason:
        raise AdapterError("web", url, "extract", reason)

    meta = trafilatura.extract_metadata(tree, default_url=url)
    title = (getattr(meta, "title", None) or urlparse(url).netloc)
    published = _parse_date(getattr(meta, "date", None))
    final_url = url

    # Articles have no timeline; one whole-document segment feeds the chunker.
    segment = TranscriptSegment(text=text, start_sec=0.0, end_sec=0.0)
    return MediaResult(
        metadata=SourceMetadata(
            platform="web", video_id=_url_id(final_url), title=title,
            url=final_url, published_at=published, duration_secs=None,
            type="article",
        ),
        transcript=[segment],
    )


JINA_READER_BASE = "https://r.jina.ai/"
# Jina's browser gives up early on slow SPAs unless told to wait — without
# X-Timeout it returns 200 with a "not yet fully loaded" warning and an
# EMPTY body (bit us on Pi 2026-07-15; cache had masked it in local tests).
_JINA_RENDER_WAIT = "30"   # seconds Jina's headless browser waits
_JINA_TIMEOUT = 75.0       # our client budget: render wait + transfer slack


def fetch_via_jina(url: str) -> MediaResult:
    """Cloud-render fallback for JS-only pages (rule 2 amendment 2026-07-15,
    off by default; the admin toggle gates every call).

    The PUBLIC page URL is sent to the third-party Jina Reader, which renders
    it in a real browser and returns plain text with Title/URL header lines.
    Output is untrusted content like any article body (rule 12-1). SSRF note:
    the only host we connect to is r.jina.ai itself; the target URL rides in
    the path, and Jina fetches it from its own network — our private ranges
    are unreachable through it and the URL was already scheme-checked."""
    # Default (markdown) format carries "Title:"/"URL Source:" header lines;
    # the bare text format does not (verified 2026-07-15).
    headers = {"User-Agent": _UA, "Accept-Language": "zh-TW,zh,en;q=0.8",
               "X-Timeout": _JINA_RENDER_WAIT}
    # Streamed with a hard BYTE cap, mirroring _stream_capped — a non-stream
    # get() would materialize an unbounded body before slicing (audit 🔵).
    try:
        with httpx.stream("GET", JINA_READER_BASE + url, headers=headers,
                          timeout=_JINA_TIMEOUT, follow_redirects=False) as resp:
            if resp.status_code != 200:
                raise AdapterError("web", url, "jina",
                                   f"雲端渲染服務回應 HTTP {resp.status_code}")
            total = 0
            parts: list[bytes] = []
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_ARTICLE_BYTES:
                    break  # keep what we have; article cap, not an error
                parts.append(chunk)
    except httpx.HTTPError as exc:
        raise AdapterError("web", url, "jina", f"雲端渲染服務連線失敗:{exc}") from exc
    raw = b"".join(parts).decode("utf-8", errors="replace")

    # Text format opens with metadata lines: "Title: ...\nURL Source: ...\n
    # (optional lines)\n\n<body>". Parse defensively; missing header = body only.
    # Peel leading metadata lines (Title/URL Source/Published Time/...);
    # body starts after the "Markdown Content:" marker. Only peel when that
    # marker is actually present — otherwise a body that legitimately opens
    # with "Title:" prose would be eaten (review S3). Verified live 2026-07-15.
    _META_RE = re.compile(
        r"^(Title|URL Source|Published Time|Warning|Markdown Content):\s*(.*)")
    title = None
    published = None
    if "Markdown Content:" in raw[:2000]:
        lines = raw.splitlines()
        i = 0
        while i < len(lines):
            ln = lines[i].strip()
            if not ln:
                i += 1
                continue
            m = _META_RE.match(ln)
            if not m:
                break
            if m.group(1) == "Title" and m.group(2).strip():
                title = m.group(2).strip()
            elif m.group(1) == "Published Time" and m.group(2).strip():
                # Rule 13: keep the publish date when Jina hands it to us
                # instead of always falling back to 未知 (review W2).
                published = _parse_date(m.group(2).strip())
            i += 1
            if m.group(1) == "Markdown Content":
                break
        text = "\n".join(lines[i:]).strip()
    else:
        text = raw.strip()
    reason = _wall_reason(text, min_chars=MIN_RENDERED_CHARS)
    if reason:
        raise AdapterError("web", url, "jina", f"雲端渲染後仍{reason}")

    segment = TranscriptSegment(text=text, start_sec=0.0, end_sec=0.0)
    return MediaResult(
        metadata=SourceMetadata(
            platform="web", video_id=_url_id(url),
            title=title or urlparse(url).netloc, url=url,
            published_at=published, duration_secs=None, type="article",
        ),
        transcript=[segment],
    )


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def _url_id(url: str) -> str:
    """Stable per-URL id for dedup (path without query/fragment)."""
    p = urlparse(url)
    return f"{p.netloc}{p.path}".rstrip("/")
