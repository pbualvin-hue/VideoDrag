"""One-shot regression runner for all offline verify_*.py scripts.

Run before deploy / after any change:
    .venv/Scripts/python -X utf8 scripts/run_all_tests.py

Each sub-script self-reports PASS/FAIL and exits non-zero on failure. Scripts
with external needs (ffmpeg, ANTHROPIC_API_KEY, a populated test db, network)
are run only when their prerequisites are present, else SKIPPED with a note.
Exit code is non-zero if any RUN script failed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

HAS_FFMPEG = bool(shutil.which("ffmpeg") or os.environ.get("FFMPEG_PATH"))
HAS_ANTHROPIC = bool(os.environ.get("ANTHROPIC_API_KEY"))
TEST_DB = Path(os.environ.get("VIDRAG_DATA_DIR", "")) / "vidrag.db" \
    if os.environ.get("VIDRAG_DATA_DIR") else None
HAS_TEST_DB = bool(TEST_DB and TEST_DB.exists())

# (script, always_run, prereq_ok, skip_reason)
SUITE = [
    ("verify_db.py", True, True, ""),
    ("verify_normalize.py", True, True, ""),
    ("verify_rag.py", True, True, ""),
    ("verify_pipeline_atomicity.py", True, True, ""),
    ("verify_transcribe_offline.py", False, HAS_FFMPEG, "需要 ffmpeg"),
    ("verify_article.py", False, True, ""),  # network; fails loud if offline
    ("verify_chat_offline.py", False, HAS_TEST_DB,
     "需要 VIDRAG_DATA_DIR 指向已入庫的測試 db"),
    ("verify_vision.py", False, HAS_FFMPEG, "需要 ffmpeg(讀圖另需 ANTHROPIC_API_KEY)"),
    ("eval_retrieval.py", False, bool(os.environ.get("RUN_EVAL")),
     "檢索評測;設 RUN_EVAL=1 開啟(使用本地 fastembed 模型,較慢)"),
]


def run(script: str) -> bool:
    proc = subprocess.run(
        [PY, "-X", "utf8", str(ROOT / "scripts" / script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    ok = proc.returncode == 0
    tail = [ln for ln in proc.stdout.splitlines()
            if ln.startswith(("PASS", "FAIL", "ALL", "FAILED", "SKIP"))]
    print(f"\n=== {script}: {'OK' if ok else 'FAILED'} ===")
    for ln in tail[-14:]:
        print("  " + ln)
    if not ok and not tail:
        print("  " + (proc.stderr.strip().splitlines() or ["(no output)"])[-1])
    return ok


def main() -> int:
    print(f"env: ffmpeg={HAS_FFMPEG} anthropic_key={HAS_ANTHROPIC} "
          f"test_db={HAS_TEST_DB}")
    ran, passed, skipped = [], [], []
    for script, always, prereq_ok, reason in SUITE:
        if not always and not prereq_ok:
            skipped.append((script, reason))
            print(f"\n=== {script}: SKIPPED ({reason}) ===")
            continue
        ran.append(script)
        if run(script):
            passed.append(script)

    print("\n" + "=" * 50)
    print(f"RAN {len(ran)} | PASSED {len(passed)} | "
          f"FAILED {len(ran) - len(passed)} | SKIPPED {len(skipped)}")
    for s, r in skipped:
        print(f"  skip: {s} — {r}")
    failed = [s for s in ran if s not in passed]
    if failed:
        print(f"\nFAILURES: {failed}")
        return 1
    print("\nALL RUN SUITES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
