# Acceptance check for Phase1-1 (config) and Phase1-2 (db.py).
# Run: .venv/Scripts/python scripts/verify_db.py
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import ConfigError, load_settings

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


# --- config: missing key must raise a clear error ---
import os

os.environ.pop("GROQ_API_KEY", None)
try:
    load_settings(require_keys=True)
    check("config missing-key error", False, "no error raised")
except ConfigError as e:
    check("config missing-key error", "GROQ_API_KEY" in str(e), str(e))

s = load_settings(require_keys=False)
check("config key-free load", s.db_path.name == "vidrag.db", str(s.db_path))

# --- opencc s2twp sanity ---
from opencc import OpenCC

cc = OpenCC("s2twp")
out = cc.convert("软件内存优化")
check("opencc s2twp", out == "軟體記憶體最佳化", out)

# --- db: pragmas, schema, vec, model gate, cascade ---
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / "t.db"
    conn = db.connect(path)
    check("WAL mode", conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
    check(
        "busy_timeout",
        conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000,
    )
    db.init_schema(conn)

    db.ensure_embedding_model(conn, "model-A", 4)
    conn.execute(
        "INSERT INTO sources (platform, video_id, url_original, url_normalized,"
        " ingested_at) VALUES ('youtube', 'abc', 'u', 'u', ?)",
        (db.utcnow_iso(),),
    )
    sid = conn.execute("SELECT source_id FROM sources").fetchone()[0]
    conn.execute(
        "INSERT INTO chunks (source_id, text, start_sec, end_sec)"
        " VALUES (?, 'hello', 0, 10)",
        (sid,),
    )
    cid = conn.execute("SELECT chunk_id FROM chunks").fetchone()[0]
    import struct

    vec = struct.pack("4f", 0.1, 0.2, 0.3, 0.4)
    conn.execute(
        "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)", (cid, vec)
    )
    knn = conn.execute(
        "SELECT chunk_id FROM chunk_vectors WHERE embedding MATCH ? AND k = 1",
        (vec,),
    ).fetchall()
    check("sqlite-vec KNN roundtrip", [r[0] for r in knn] == [cid])

    # same model → ok; different model → refuse
    db.ensure_embedding_model(conn, "model-A", 4)
    try:
        db.ensure_embedding_model(conn, "model-B", 4)
        check("model mismatch gate", False, "no error raised")
    except db.ModelMismatchError as e:
        check("model mismatch gate", "重建" in str(e))

    # cascade delete removes chunks + vectors
    db.delete_source(conn, sid)
    check(
        "cascade delete",
        conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        and conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0,
    )

    # timestamps are UTC ISO-8601 with Z suffix
    check("UTC timestamp format", db.utcnow_iso().endswith("Z"), db.utcnow_iso())
    conn.close()

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
