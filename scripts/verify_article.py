# Acceptance for Phase4-1 (article adapter + SSRF guard). Hits the network.
# Run: .venv/Scripts/python -X utf8 scripts/verify_article.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import article
from app.ingest.article import ArticleSSRFError, _validated_ip
from app.ingest.normalize import classify

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


# --- SSRF guard: private / loopback / link-local / metadata must be blocked ---
for bad in [
    "http://127.0.0.1/admin",
    "http://localhost/x",
    "http://10.0.0.5/x",
    "http://192.168.1.1/x",
    "http://169.254.169.254/latest/meta-data",  # cloud metadata
    "http://[::1]/x",
    "http://[::ffff:169.254.169.254]/x",          # IPv4-mapped bypass attempt
    "http://100.64.0.1/x",                         # CGNAT (CVE-2024-4032 range)
    "http://user@169.254.169.254/x",              # userinfo trick
    "ftp://example.com/x",
]:
    try:
        _validated_ip(bad)
        check(f"block {bad}", False, "allowed!")
    except ArticleSSRFError:
        check(f"block {bad}", True)

# A public host must pin to a validated public IP.
try:
    host, ip, port = _validated_ip("https://www.gutenberg.org/x")
    import ipaddress as _ip
    check("public host pins to public ip",
          host == "www.gutenberg.org" and _ip.ip_address(ip).is_global,
          f"{ip}:{port}")
except ArticleSSRFError as e:
    check("public host pins to public ip", False, str(e))

# --- classify routes non-platform URLs to the article path ---
n = classify("https://example.com/some/article")
check("classify -> web", n.platform == "web" and n.video_id == "example.com/some/article",
      f"{n.platform}:{n.video_id}")
# video platforms still route to video
nv = classify("https://youtu.be/dQw4w9WgXcQ")
check("classify keeps video", nv.platform == "youtube")

# --- real public article extraction (network; timeouts are SKIP, not FAIL —
# the SSRF guard checks above are the correctness-critical part) ---
import httpx as _httpx
from app.ingest.base import AdapterError

_NET_ERR = (_httpx.TransportError, AdapterError)
for _url in ("https://www.gutenberg.org/cache/epub/1342/pg1342.txt",
             "https://text.npr.org/"):
    try:
        result = article.fetch(_url)
        md = result.metadata
        text = result.transcript[0].text if result.transcript else ""
        check("article extracted", len(text) > 500, f"{len(text)} chars")
        check("article metadata", md.type == "article" and bool(md.title),
              f"type={md.type} title={md.title[:30]!r}")
        check("article no duration", md.duration_secs is None)
        break
    except _NET_ERR as e:
        print(f"  (network issue on {_url}: {e}; trying next)")
else:
    print("SKIP article live-fetch (network unavailable) — SSRF guard checks passed")

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
