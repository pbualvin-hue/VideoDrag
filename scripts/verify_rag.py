# Acceptance checks for Phase1-6 (chunker) and Phase1-7 (embedder).
# Run: .venv/Scripts/python scripts/verify_rag.py
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import db
from app.ingest.base import TranscriptSegment
from app.rag.chunker import MAX_TOKENS, MIN_TOKENS, Chunk, chunk_transcript, count_tokens
from app.rag.embedder import Embedder, vector_to_blob

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


# --- chunker: synthetic Chinese transcript, ~40 tokens per segment ---
segments = [
    TranscriptSegment(
        text=f"第{i}句,台積電先進製程的產能利用率持續攀升,市場對高效能運算需求強勁。",
        start_sec=i * 10.0,
        end_sec=i * 10.0 + 9.0,
    )
    for i in range(60)
]
chunks = chunk_transcript(segments)
check("chunker produces chunks", len(chunks) > 1, f"{len(chunks)} chunks")

sizes = [count_tokens(c.text) for c in chunks]
check(
    "all chunks <= MAX_TOKENS",
    all(s <= MAX_TOKENS for s in sizes),
    str(sizes),
)
check(
    "non-final chunks >= MIN_TOKENS",
    all(s >= MIN_TOKENS for s in sizes[:-1]),
    str(sizes[:-1]),
)
check(
    "timestamps monotonic",
    all(c.start_sec < c.end_sec for c in chunks)
    and all(chunks[i].start_sec < chunks[i + 1].start_sec for i in range(len(chunks) - 1)),
)
# overlap: consecutive chunks share text (the 15% tail)
overlaps = [
    chunks[i].text[-30:] in chunks[i + 1].text for i in range(len(chunks) - 1)
]
check("15% overlap present", all(overlaps), str(overlaps))
# full coverage: every segment's text appears in some chunk
check(
    "no content lost",
    all(any(seg.text in c.text for c in chunks) for seg in segments),
)

# oversize single segment gets sentence-split, not dropped
huge = TranscriptSegment(
    text="超長句子。" * 200, start_sec=0.0, end_sec=100.0
)
huge_chunks = chunk_transcript([huge])
check(
    "oversize segment split",
    len(huge_chunks) > 1 and all(count_tokens(c.text) <= MAX_TOKENS for c in huge_chunks),
    f"{len(huge_chunks)} chunks",
)

# --- embedder: MiniLM candidate (downloads model on first run) ---
emb = Embedder("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
check("dim probed", emb.dim == 384, f"dim={emb.dim}")

with_title = emb.embed_chunks("台積電法說會", ["產能利用率上升"])[0]
without_title = emb.embed_texts(["產能利用率上升"])[0]
check(
    "title prefix changes embedding",
    not np.allclose(with_title, without_title),
)
q = emb.embed_query("台積電產能")
check("query embed works", q.shape == (384,), str(q.shape))

# --- end-to-end vec storage roundtrip via db ---
with tempfile.TemporaryDirectory() as td:
    conn = db.connect(Path(td) / "t.db")
    db.init_schema(conn)
    db.ensure_embedding_model(conn, emb.model_name, emb.dim)
    conn.execute(
        "INSERT INTO sources (platform, video_id, url_original, url_normalized,"
        " ingested_at) VALUES ('youtube', 'x', 'u', 'u', ?)",
        (db.utcnow_iso(),),
    )
    sid = conn.execute("SELECT source_id FROM sources").fetchone()[0]
    texts = ["台積電產能利用率上升", "今天天氣很好適合出遊"]
    vecs = emb.embed_chunks("台積電法說會重點", texts)
    for text, vec in zip(texts, vecs):
        cur = conn.execute(
            "INSERT INTO chunks (source_id, text, start_sec, end_sec)"
            " VALUES (?, ?, 0, 1)",
            (sid, text),
        )
        conn.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
            (cur.lastrowid, vector_to_blob(vec)),
        )
    conn.commit()
    hit = conn.execute(
        "SELECT c.text FROM chunk_vectors v JOIN chunks c ON c.chunk_id = v.chunk_id"
        " WHERE v.embedding MATCH ? AND k = 1",
        (vector_to_blob(emb.embed_query("晶圓廠產能")),),
    ).fetchone()
    check(
        "semantic KNN returns relevant chunk",
        hit is not None and "產能" in hit[0],
        repr(hit[0] if hit else None),
    )
    conn.close()

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
