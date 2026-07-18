# Acceptance check for Phase1-3 (normalize.py).
# Run: .venv/Scripts/python scripts/verify_normalize.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.normalize import (
    NormalizeError,
    expand_url,
    parse_video_url,
    resolve,
)

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


VID = "dQw4w9WgXcQ"
same_video_forms = [
    f"https://youtu.be/{VID}",
    f"https://www.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/watch?v={VID}&utm_source=share&si=tracking123",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://m.youtube.com/watch?v={VID}",
]
results = {parse_video_url(u) for u in same_video_forms}
check(
    "4+ URL forms -> one identity",
    len(results) == 1
    and results.pop().canonical_url == f"https://www.youtube.com/watch?v={VID}",
    str([f"{r.platform}:{r.video_id}" for r in results] or "unified"),
)

check(
    "tiktok parse",
    parse_video_url("https://www.tiktok.com/@user/video/7300000000000000000").video_id
    == "7300000000000000000",
)
check(
    "instagram reel parse",
    parse_video_url("https://www.instagram.com/reel/C8abcDEfGhi/?igsh=xyz").video_id
    == "C8abcDEfGhi",
)

# --- rejections ---
for bad in [
    "https://evil.com/watch?v=" + VID,
    "https://youtube.com.evil.com/watch?v=" + VID,  # suffix spoof
    "ftp://youtube.com/watch?v=" + VID,
    "https://www.youtube.com/watch?v=short",  # malformed id
    "file:///etc/passwd",
]:
    try:
        parse_video_url(bad)
        check(f"reject {bad}", False, "accepted!")
    except NormalizeError:
        check(f"reject {bad}", True)

# --- redirect expansion with injected head() ---
def make_head(chain: dict[str, str]):
    def head(url: str):
        if url in chain:
            return 301, chain[url]
        return 200, None

    return head


# vt.tiktok.com style: short link -> full URL in 1 hop
short = "https://vt.tiktok.com/ZSxyz/"
full = "https://www.tiktok.com/@user/video/7300000000000000000"
check(
    "short link expands",
    expand_url(short, head=make_head({short: full})) == full,
)

# redirect to off-whitelist host must be refused
try:
    expand_url(short, head=make_head({short: "https://evil.com/x"}))
    check("refuse off-whitelist redirect", False, "followed!")
except NormalizeError:
    check("refuse off-whitelist redirect", True)

# >3 hops must be refused
chain = {
    "https://vt.tiktok.com/a": "https://vt.tiktok.com/b",
    "https://vt.tiktok.com/b": "https://vt.tiktok.com/c",
    "https://vt.tiktok.com/c": "https://vt.tiktok.com/d",
    "https://vt.tiktok.com/d": "https://vt.tiktok.com/e",
}
try:
    expand_url("https://vt.tiktok.com/a", head=make_head(chain))
    check("refuse >3 hops", False, "followed!")
except NormalizeError:
    check("refuse >3 hops", True)

# exactly 3 hops is allowed
chain3 = {
    "https://vt.tiktok.com/a": "https://vt.tiktok.com/b",
    "https://vt.tiktok.com/b": "https://vt.tiktok.com/c",
    "https://vt.tiktok.com/c": full,
}
check(
    "3 hops ok",
    expand_url("https://vt.tiktok.com/a", head=make_head(chain3)) == full,
)

# resolve(): direct parse needs no network (head fn would blow up if called)
check("resolve without network", resolve(f"https://youtu.be/{VID}").video_id == VID)

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
