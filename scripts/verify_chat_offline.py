# Offline acceptance checks for Phase 2 (no ANTHROPIC_API_KEY needed):
# hybrid retrieval, FTS backfill, sessions, no-hit path, summary enqueue.
# Run against the test data dir:
#   VIDRAG_DATA_DIR=... .venv/Scripts/python -X utf8 scripts/verify_chat_offline.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import load_settings
from app.rag import chat
from app.rag.embedder import Embedder
from app.rag.summarize import enqueue_summary

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


settings = load_settings(require_keys=False)
conn = db.connect(settings.db_path)
db.init_schema(conn)  # exercises the FTS backfill path on the pre-FTS db

n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
n_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
check("FTS backfill", n_chunks == n_fts and n_chunks > 0,
      f"chunks={n_chunks} fts={n_fts}")

hit = conn.execute(
    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"CPO\"'"
).fetchall()
check("FTS exact ticker/term match", len(hit) >= 1, f"{len(hit)} rows")

emb = Embedder(db.get_meta(conn, "embedding_model"))
db.ensure_embedding_model(conn, emb.model_name, emb.dim)

# Resolve source ids by title so the checks survive re-ingests / id shifts.
def sid_like(substr):
    row = conn.execute(
        "SELECT source_id FROM sources WHERE title LIKE ?", (f"%{substr}%",)
    ).fetchone()
    return row["source_id"] if row else None

fin_sid = sid_like("盤中連線")   # 台股 analysis video
edu_sid = sid_like("schools kill creativity")  # TED talk

# Finance question should hit the 台股 video, not the TED talk.
r1 = chat.retrieve(conn, emb, "今天台股個股輪動的情況怎麼樣?")
check("hybrid retrieval finance -> 台股 video",
      bool(r1) and r1[0].source_id == fin_sid,
      f"top={r1[0].source_id if r1 else None} want={fin_sid}" if r1 else "no hits")

# Education question should hit the TED talk.
r2 = chat.retrieve(conn, emb, "學校教育會扼殺小孩的創造力嗎?")
check("hybrid retrieval education -> TED",
      bool(r2) and r2[0].source_id == edu_sid,
      f"top={r2[0].source_id if r2 else None} want={edu_sid}" if r2 else "no hits")

# Scope filter: finance question restricted to the TED source must not return
# the finance video's chunks.
r3 = chat.retrieve(conn, emb, "今天台股個股輪動的情況怎麼樣?", source_id=edu_sid)
check("scope filter", all(c.source_id == edu_sid for c in r3), f"{len(r3)} chunks")

# Retrieval must carry the citation triple: title, timestamps, published date.
c = r1[0]
check("citation triple present",
      bool(c.title) and c.start_sec is not None and bool(c.published_at),
      f"{c.title[:20]} @{chat.format_timestamp(c.start_sec)} {c.published_at}")

# No-hit path answers without an API key and persists messages.
sid = chat.new_session_id()
res = chat.answer(conn, settings, emb, "量子重力理論的最新進展?", sid)
check("no-hit canned answer", "沒有" in res.answer, res.answer[:40])
msgs = conn.execute(
    "SELECT role FROM messages WHERE session_id = ? ORDER BY msg_id", (sid,)
).fetchall()
check("messages persisted", [m["role"] for m in msgs] == ["user", "assistant"])

# Session helpers.
check("latest_session_id", chat.latest_session_id(conn) == sid)
check("list_sessions", any(r["session_id"] == sid for r in chat.list_sessions(conn)))

# Summary enqueue is idempotent — use a throwaway source so the check
# doesn't depend on whether live runs already summarized real sources.
cur = conn.execute(
    "INSERT INTO sources (platform, video_id, url_original, url_normalized,"
    " ingested_at, status) VALUES ('youtube', '__enqueue_test__', 'u', 'u', ?,"
    " 'ready')",
    (db.utcnow_iso(),),
)
tmp_sid = cur.lastrowid
first = enqueue_summary(conn, tmp_sid)
second = enqueue_summary(conn, tmp_sid)
n_jobs = conn.execute(
    "SELECT COUNT(*) FROM jobs WHERE kind='summarize' AND source_id=?",
    (tmp_sid,),
).fetchone()[0]
check("summary enqueue idempotent", first and not second and n_jobs == 1,
      f"first={first} second={second} jobs={n_jobs}")
conn.execute("DELETE FROM jobs WHERE source_id = ?", (tmp_sid,))
db.delete_source(conn, tmp_sid)
conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
conn.commit()

# format_timestamp sanity.
check("timestamp format", chat.format_timestamp(754) == "12:34"
      and chat.format_timestamp(3723) == "1:02:03")

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
