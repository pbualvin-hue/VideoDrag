"""Container pre-flight smoke test — run inside the deployed container as the
first post-build check:  docker compose exec vidrag python scripts/preflight.py

Confirms every native component actually works on the target arch (arm64 on
the Pi), before you rely on the app. Exits non-zero on any failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

checks: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    checks.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


# --- external binaries on PATH ---
ff = shutil.which("ffmpeg")
check("ffmpeg on PATH", ff is not None, ff or "not found")
deno = shutil.which("deno")
check("deno on PATH (yt-dlp JS runtime)", deno is not None, deno or "not found")

# --- python arch ---
import platform

check("64-bit arch", platform.machine() in ("aarch64", "arm64", "x86_64", "AMD64"),
      platform.machine())
check("python >= 3.12.4 (article SSRF CVE-2024-4032)",
      sys.version_info >= (3, 12, 4), sys.version.split()[0])

# --- native python deps ---
try:
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    c.execute("INSERT INTO t VALUES ('台積電法說會')")
    hit = c.execute("SELECT * FROM t WHERE t MATCH '積電法'").fetchall()
    check("sqlite FTS5 trigram (CJK)", len(hit) == 1)
except Exception as e:
    check("sqlite FTS5 trigram (CJK)", False, str(e))

try:
    import sqlite_vec
    c = sqlite3.connect(":memory:")
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    c.execute("CREATE VIRTUAL TABLE v USING vec0(id INTEGER PRIMARY KEY, e float[4])")
    check("sqlite-vec loads + vec0 table", True)
except Exception as e:
    check("sqlite-vec loads + vec0 table", False, str(e))

try:
    from opencc import OpenCC
    ok = OpenCC("s2twp").convert("软件内存") == "軟體記憶體"
    check("opencc s2twp", ok)
except Exception as e:
    check("opencc s2twp", False, str(e))

try:
    from fastembed import TextEmbedding
    m = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5")
    dim = len(next(iter(m.embed(["測試"]))))
    check("fastembed bge-small-zh (onnxruntime)", dim == 512, f"dim={dim}")
except Exception as e:
    check("fastembed bge-small-zh (onnxruntime)", False, str(e))

# --- ffmpeg actually runs (not just present) ---
if ff:
    try:
        r = subprocess.run([ff, "-f", "lavfi", "-i", "sine=d=1", "-f", "null", "-"],
                           capture_output=True, timeout=30)
        check("ffmpeg executes", r.returncode == 0)
    except Exception as e:
        check("ffmpeg executes", False, str(e))

# --- yt-dlp sees deno ---
try:
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True, text=True, timeout=30)
    check("yt-dlp runnable", r.returncode == 0, r.stdout.strip())
except Exception as e:
    check("yt-dlp runnable", False, str(e))

print()
failed = [n for n, ok, _ in checks if not ok]
if failed:
    print(f"PRE-FLIGHT FAILED: {failed}")
    sys.exit(1)
print("PRE-FLIGHT ALL PASSED — container is deployment-ready")
