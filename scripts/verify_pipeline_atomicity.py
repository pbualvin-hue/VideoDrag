# Regression check for review Critical-1: a mid-ingest failure must leave
# no orphan chunks/vectors and mark the source failed.
# Run: .venv/Scripts/python -X utf8 scripts/verify_pipeline_atomicity.py
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import db
from app.config import Settings
from app.ingest import pipeline
from app.ingest.base import MediaResult, SourceMetadata, TranscriptSegment

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


class StubEmbedder:
    """Returns one valid vector, then a wrong-dimension one -> the second
    chunk_vectors INSERT fails after the first vector is already written."""

    model_name = "stub-model"
    dim = 4

    def embed_chunks(self, title: str, texts: list[str]) -> list[np.ndarray]:
        vecs = [np.zeros(4, dtype=np.float32) for _ in texts]
        if len(vecs) > 1:
            vecs[-1] = np.zeros(3, dtype=np.float32)  # dim mismatch -> INSERT raises
        return vecs


def fake_fetch(norm, workdir, max_duration_secs):
    segments = [
        TranscriptSegment(text=f"第{i}句,足夠長的測試內容讓切塊器產生多個 chunk。" * 20,
                          start_sec=i * 10.0, end_sec=i * 10.0 + 9.0)
        for i in range(4)
    ]
    return MediaResult(
        metadata=SourceMetadata(
            platform="youtube", video_id=norm.video_id, title="測試影片",
            url=norm.canonical_url, published_at="2026-01-01T00:00:00Z",
            duration_secs=40,
        ),
        transcript=segments,
    )


with tempfile.TemporaryDirectory() as td:
    data_dir = Path(td)
    settings = Settings(
        data_dir=data_dir, db_path=data_dir / "t.db", groq_api_key="",
        anthropic_api_key="", app_token="", mcp_token="", monthly_budget_usd=5,
        max_video_duration_secs=10800, chat_model_mode="accurate",
        embedding_model="stub-model",
    )
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    original_fetch = pipeline.dispatcher.fetch
    pipeline.dispatcher.fetch = fake_fetch
    try:
        try:
            pipeline.ingest_url(
                "https://youtu.be/dQw4w9WgXcQ", settings, conn, StubEmbedder()
            )
            check("ingest fails on bad vector", False, "no error raised")
        except Exception as e:
            check("ingest fails on bad vector", True, type(e).__name__)

        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_vecs = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
        status = conn.execute("SELECT status FROM sources").fetchone()[0]
        check("no orphan chunks after failure", n_chunks == 0, f"{n_chunks}")
        check("no orphan vectors after failure", n_vecs == 0, f"{n_vecs}")
        check("source marked failed", status == "failed", status)
        check("tmp workdir cleaned",
              not any((data_dir / "tmp").iterdir()) if (data_dir / "tmp").exists() else True)
    finally:
        pipeline.dispatcher.fetch = original_fetch
    conn.close()

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
