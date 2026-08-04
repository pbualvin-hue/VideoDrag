"""Synchronous ingest pipeline: URL -> transcript -> chunks -> vectors -> db.

Used by the Phase 1 CLI directly and by the Phase 3 background worker.
Audio files are temporary: deleted after transcription or on failure
(CLAUDE.md rule 17). Duplicate shares return instantly without re-fetching
(rule 11).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
from dataclasses import dataclass

from .. import db
from ..config import Settings
from ..textnorm import s2twp
from ..rag.chunker import chunk_transcript
from ..rag.embedder import Embedder, vector_to_blob
from . import article, dispatcher
from .base import TranscriptSegment
from .normalize import classify
from .transcribe import has_audio_stream, transcribe

logger = logging.getLogger(__name__)

# Groq whisper-large-v3 list price per hour of audio; free tier bills $0
# but we record the reference cost for /stats transparency.
GROQ_WHISPER_USD_PER_HOUR = 0.111


@dataclass(frozen=True)
class IngestOutcome:
    source_id: int
    status: str            # ready / duplicate
    chunk_count: int = 0


def resolve_embedding_model(conn: sqlite3.Connection, settings: Settings) -> str:
    """meta-recorded model wins over config default (rule 14); an explicit
    EMBEDDING_MODEL override that mismatches meta will refuse startup in
    ensure_embedding_model."""
    return db.get_meta(conn, "embedding_model") or settings.embedding_model


def _finalize_display_title(conn, settings, source_id) -> None:
    """Best-effort AI topic name for the library card. The source is already
    ready — a naming failure must never fail the ingest; the startup backfill
    retries anything left unnamed."""
    try:
        from ..rag.summarize import generate_display_title
        generate_display_title(conn, settings, source_id)
    except Exception as exc:  # noqa: BLE001 — cosmetic, never fatal
        logger.warning("display-title failed for source %s: %s", source_id, exc)


def ingest_url(
    url: str,
    settings: Settings,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
) -> IngestOutcome:
    norm = classify(url)
    is_article = norm.platform == "web"

    existing = conn.execute(
        "SELECT source_id, status FROM sources WHERE platform = ? AND video_id = ?",
        (norm.platform, norm.video_id),
    ).fetchone()
    if existing and existing["status"] != "failed":
        logger.info("duplicate share: source_id=%s", existing["source_id"])
        return IngestOutcome(existing["source_id"], "duplicate")
    if existing:
        # A failed record must not block a retry (dedup is for content,
        # not for failures); purge it and ingest fresh.
        logger.info("re-ingesting failed source_id=%s", existing["source_id"])
        db.delete_source(conn, existing["source_id"])

    # Embedder is created only after the dedup check so duplicates return
    # instantly without touching the (potentially cold) model cache.
    if embedder is None:
        embedder = Embedder(resolve_embedding_model(conn, settings))
    db.ensure_embedding_model(conn, embedder.model_name, embedder.dim)

    src_type = "article" if is_article else "video"
    cur = conn.execute(
        "INSERT INTO sources (type, platform, video_id, url_original,"
        " url_normalized, ingested_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, 'processing')",
        (src_type, norm.platform, norm.video_id, url, norm.canonical_url,
         db.utcnow_iso()),
    )
    source_id = cur.lastrowid
    conn.commit()

    modality = "article" if is_article else "audio"
    # Filesystem-safe workdir key (article ids contain slashes).
    safe_key = norm.video_id.replace("/", "_").replace(":", "_")
    workdir = settings.data_dir / "tmp" / f"{norm.platform}_{safe_key}"
    inserted_vector_ids: list[int] = []
    try:
        if is_article:
            try:
                media = article.fetch(norm.canonical_url)
            except article.AdapterError as exc:
                # Cloud-render retry (rule 2 amendment, user-toggled) for:
                # (a) extract-stage: JS-only pages leave no server-side body;
                # (b) fetch-stage HTTP 403: bot walls that reject our client
                #     but serve a real browser (小修 2026-07-17,實例
                #     sounds.spriters-resource.com)。SSRF/dns/scheme stages
                #     never reach here with these markers.
                retryable = exc.stage == "extract" or (
                    exc.stage == "fetch" and "HTTP 403" in exc.original)
                if settings.jina_fallback and retryable:
                    logger.info("article %s failed; retrying via jina: %s",
                                exc.stage, norm.canonical_url)
                    media = article.fetch_via_jina(norm.canonical_url)
                else:
                    raise
        else:
            media = dispatcher.fetch(
                norm, workdir, settings.max_video_duration_secs
            )
        title = s2twp.convert(media.metadata.title)

        if media.has_images:
            # IG image/carousel post (rule 1: type=image). No audio/transcript
            # path applies — caption + vision produce the chunks.
            n = _ingest_image_post(conn, settings, embedder, source_id,
                                   title, media, workdir, inserted_vector_ids)
            conn.execute(
                "UPDATE sources SET type = 'image', title = ?,"
                " published_at = ?, status = 'ready' WHERE source_id = ?",
                (title, media.metadata.published_at, source_id),
            )
            conn.commit()
            _finalize_display_title(conn, settings, source_id)
            logger.info("image source %s ready: %d chunks", source_id, n)
            return IngestOutcome(source_id, "ready", n)

        if media.audio_path and not has_audio_stream(media.audio_path):
            # Silent video (no audio stream, e.g. an IG slideshow reel): rule 3
            # says on-screen content ⇒ vision. Read the local file's frames
            # directly instead of hard-failing the audio preprocess.
            n = _ingest_silent_video(conn, settings, embedder, source_id,
                                     title, media, workdir)
            conn.execute(
                "UPDATE sources SET title = ?, published_at = ?,"
                " duration_secs = ?, status = 'ready' WHERE source_id = ?",
                (title, media.metadata.published_at,
                 media.metadata.duration_secs, source_id),
            )
            conn.commit()
            _finalize_display_title(conn, settings, source_id)
            logger.info("silent-video source %s ready: %d visual chunks",
                        source_id, n)
            return IngestOutcome(source_id, "ready", n)

        if media.has_transcript:
            # Official captions and article text bypass transcribe.py, which
            # is where s2twp normally runs — convert here so simplified-Chinese
            # captions/articles still land as Taiwan Traditional (rule 3).
            segments = [
                TranscriptSegment(text=s2twp.convert(seg.text),
                                  start_sec=seg.start_sec, end_sec=seg.end_sec)
                for seg in media.transcript
            ]
        else:
            if not settings.groq_api_key:
                raise RuntimeError(
                    "此影片沒有官方字幕,需要 GROQ_API_KEY 進行轉錄:"
                    "請在 data/.env 填入後重試"
                )
            result = transcribe(
                media.audio_path, workdir, settings.groq_api_key,
                vocab_path=settings.vocabulary_path,
            )
            segments = result.segments
            # The Groq cost is real the moment transcription returns —
            # commit accounting immediately so a later chunk/embed failure
            # can never roll it back (成本守則).
            conn.execute(
                "INSERT INTO api_usage (ts, provider, model, cost_usd)"
                " VALUES (?, 'groq', 'whisper-large-v3', ?)",
                (db.utcnow_iso(),
                 result.audio_duration_secs / 3600 * GROQ_WHISPER_USD_PER_HOUR),
            )
            conn.commit()

        if not segments:
            raise RuntimeError("轉錄結果為空:影片可能沒有可辨識的語音內容")

        chunks = chunk_transcript(segments)
        vectors = embedder.embed_chunks(title, [c.text for c in chunks])
        chunk_ids: list[int] = []
        for chunk in chunks:
            ccur = conn.execute(
                "INSERT INTO chunks (source_id, modality, text, start_sec, end_sec)"
                " VALUES (?, ?, ?, ?, ?)",
                (source_id, modality, chunk.text, chunk.start_sec, chunk.end_sec),
            )
            chunk_ids.append(ccur.lastrowid)
        for chunk_id, vec in zip(chunk_ids, vectors):
            conn.execute(
                "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, vector_to_blob(vec)),
            )
            inserted_vector_ids.append(chunk_id)
        conn.execute(
            "UPDATE sources SET title = ?, published_at = ?, duration_secs = ?,"
            " status = 'ready' WHERE source_id = ?",
            (title, media.metadata.published_at, media.metadata.duration_secs,
             source_id),
        )
        conn.commit()

        _finalize_display_title(conn, settings, source_id)

        # Cosmetic cover thumbnail for the library card (best-effort).
        if not is_article and media.metadata.thumbnail_url:
            from ..thumbnails import download_thumbnail
            download_thumbnail(settings, source_id, media.metadata.thumbnail_url)

        # Vision is supplementary (補充制): a hollow transcript means the
        # content is on-screen, not spoken. Add visual chunks alongside the
        # audio ones. Failure here must not fail the (already-ready) source.
        if not is_article and not media.has_transcript:
            try:
                _maybe_run_vision(
                    conn, settings, embedder, source_id, norm, title,
                    segments, media.metadata.duration_secs, workdir,
                )
            except Exception as exc:  # noqa: BLE001 — supplementary, never fatal
                logger.warning("vision pass failed for source %s: %s",
                               source_id, exc)

        logger.info("source %s ready: %d chunks", source_id, len(chunks))
        return IngestOutcome(source_id, "ready", len(chunks))
    except Exception:
        conn.rollback()
        # vec0 virtual tables are not guaranteed to participate in the host
        # rollback on every sqlite-vec build — purge explicitly so a reused
        # chunk rowid can never collide with a stale vector.
        if inserted_vector_ids:
            placeholders = ",".join("?" * len(inserted_vector_ids))
            conn.execute(
                f"DELETE FROM chunk_vectors WHERE chunk_id IN ({placeholders})",
                inserted_vector_ids,
            )
        conn.execute(
            "UPDATE sources SET status = 'failed' WHERE source_id = ?",
            (source_id,),
        )
        conn.commit()
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Hard cap on a manual paste. Enforced at the API boundary too; this copy
# keeps the invariant local so no future enqueue path can bypass it.
MAX_TEXT_CHARS = 100_000
MAX_TITLE_CHARS = 200


def text_content_id(body: str) -> str:
    """Stable dedup id for manually pasted text (rule 11 analog: no URL to
    normalize, so the id is a content hash). Callers must pass the body
    AFTER s2twp conversion so 簡/繁 variants of the same text dedup."""
    return "txt_" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def ingest_text(
    text: str,
    settings: Settings,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    title: str | None = None,
) -> IngestOutcome:
    """Manual text ingest (rule 1 手動文字, 2026-07-13): content pasted into
    the PWA that has no URL — creator DMs, Threads posts, etc. Mirrors the
    article path: one whole-document segment, modality='article', no
    timeline. `title` is the optional user-supplied title; when absent the
    first line stands in until the AI display title lands."""
    body = s2twp.convert(text.strip())
    if not body:
        raise ValueError("文字內容為空")
    if len(body) > MAX_TEXT_CHARS:
        raise ValueError(f"文字超過 {MAX_TEXT_CHARS:,} 字上限")
    content_id = text_content_id(body)

    existing = conn.execute(
        "SELECT source_id, status FROM sources WHERE platform = 'manual'"
        " AND video_id = ?", (content_id,),
    ).fetchone()
    if existing and existing["status"] != "failed":
        logger.info("duplicate text paste: source_id=%s", existing["source_id"])
        return IngestOutcome(existing["source_id"], "duplicate")
    if existing:
        logger.info("re-ingesting failed source_id=%s", existing["source_id"])
        db.delete_source(conn, existing["source_id"])

    if embedder is None:
        embedder = Embedder(resolve_embedding_model(conn, settings))
    db.ensure_embedding_model(conn, embedder.model_name, embedder.dim)

    src_title = s2twp.convert(title.strip())[:MAX_TITLE_CHARS] \
        if title and title.strip() else body.splitlines()[0].strip()[:80]
    # Pasted text has no native publish date; the paste moment is the best
    # available anchor for rule-13 citations (noted as such in the UI).
    now = db.utcnow_iso()
    synthetic_url = f"manual:{content_id}"
    cur = conn.execute(
        "INSERT INTO sources (type, platform, video_id, title, url_original,"
        " url_normalized, published_at, ingested_at, status)"
        " VALUES ('text', 'manual', ?, ?, ?, ?, ?, ?, 'processing')",
        (content_id, src_title, synthetic_url, synthetic_url, now, now),
    )
    source_id = cur.lastrowid
    conn.commit()

    inserted_vector_ids: list[int] = []
    try:
        segment = TranscriptSegment(text=body, start_sec=0.0, end_sec=0.0)
        chunks = chunk_transcript([segment])
        if not chunks:
            raise RuntimeError("文字內容切塊後為空")
        vectors = embedder.embed_chunks(src_title, [c.text for c in chunks])
        for chunk, vec in zip(chunks, vectors):
            ccur = conn.execute(
                "INSERT INTO chunks (source_id, modality, text, start_sec,"
                " end_sec) VALUES (?, 'article', ?, ?, ?)",
                (source_id, chunk.text, chunk.start_sec, chunk.end_sec),
            )
            conn.execute(
                "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                (ccur.lastrowid, vector_to_blob(vec)),
            )
            inserted_vector_ids.append(ccur.lastrowid)
        conn.execute(
            "UPDATE sources SET status = 'ready' WHERE source_id = ?",
            (source_id,),
        )
        conn.commit()

        _finalize_display_title(conn, settings, source_id)

        logger.info("text source %s ready: %d chunks", source_id, len(chunks))
        return IngestOutcome(source_id, "ready", len(chunks))
    except Exception:
        conn.rollback()
        # Same explicit purge as ingest_url: vec0 tables may not join the
        # host rollback, and a reused rowid must never meet a stale vector.
        if inserted_vector_ids:
            placeholders = ",".join("?" * len(inserted_vector_ids))
            conn.execute(
                f"DELETE FROM chunk_vectors WHERE chunk_id IN ({placeholders})",
                inserted_vector_ids,
            )
        conn.execute(
            "UPDATE sources SET status = 'failed' WHERE source_id = ?",
            (source_id,),
        )
        conn.commit()
        raise


def ingest_podcast(
    episode: dict,
    settings: Settings,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
) -> IngestOutcome:
    """Podcast 單集入庫(rule 1 新型態 podcast, 2026-07-17):下載 enclosure
    音訊 → 既有 Groq 轉錄管線 → chunks。episode 來自 podcast.fetch_feed 的
    集數 dict(guid/title/audio_url/link/published_at/duration_secs)+
    feed_title。"""
    from . import podcast

    content_id = podcast.episode_id(episode["guid"])
    existing = conn.execute(
        "SELECT source_id, status FROM sources WHERE platform = 'podcast'"
        " AND video_id = ?", (content_id,),
    ).fetchone()
    if existing and existing["status"] != "failed":
        logger.info("duplicate episode: source_id=%s", existing["source_id"])
        return IngestOutcome(existing["source_id"], "duplicate")
    if existing:
        db.delete_source(conn, existing["source_id"])

    if embedder is None:
        embedder = Embedder(resolve_embedding_model(conn, settings))
    db.ensure_embedding_model(conn, embedder.model_name, embedder.dim)

    title = s2twp.convert(
        f"{episode.get('feed_title', '')}:{episode['title']}".strip(":"))[:120]
    # 開原片連結:優先集數網頁,沒有就音訊檔本身(仍是可開的 URL)
    canonical = episode.get("link") or episode["audio_url"]
    cur = conn.execute(
        "INSERT INTO sources (type, platform, video_id, title, url_original,"
        " url_normalized, published_at, duration_secs, ingested_at, status)"
        " VALUES ('podcast', 'podcast', ?, ?, ?, ?, ?, ?, ?, 'processing')",
        (content_id, title, episode["audio_url"], canonical,
         episode.get("published_at"), episode.get("duration_secs"),
         db.utcnow_iso()),
    )
    source_id = cur.lastrowid
    conn.commit()

    workdir = settings.data_dir / "tmp" / f"podcast_{content_id}"
    inserted_vector_ids: list[int] = []
    try:
        audio_path = podcast.download_episode(
            episode["audio_url"], workdir,
            settings.max_video_duration_secs,
            episode.get("duration_secs"),
        )
        if not settings.groq_api_key:
            raise RuntimeError(
                "Podcast 需要 GROQ_API_KEY 進行轉錄:請在管理頁填入後重試")
        result = transcribe(
            audio_path, workdir, settings.groq_api_key,
            vocab_path=settings.vocabulary_path,
        )
        # Cost is real the moment transcription returns (成本守則).
        conn.execute(
            "INSERT INTO api_usage (ts, provider, model, cost_usd)"
            " VALUES (?, 'groq', 'whisper-large-v3', ?)",
            (db.utcnow_iso(),
             result.audio_duration_secs / 3600 * GROQ_WHISPER_USD_PER_HOUR),
        )
        conn.commit()
        if not result.segments:
            raise RuntimeError("轉錄結果為空:這集可能沒有可辨識的語音內容")

        chunks = chunk_transcript(result.segments)
        vectors = embedder.embed_chunks(title, [c.text for c in chunks])
        for chunk, vec in zip(chunks, vectors):
            ccur = conn.execute(
                "INSERT INTO chunks (source_id, modality, text, start_sec,"
                " end_sec) VALUES (?, 'audio', ?, ?, ?)",
                (source_id, chunk.text, chunk.start_sec, chunk.end_sec),
            )
            conn.execute(
                "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                (ccur.lastrowid, vector_to_blob(vec)),
            )
            inserted_vector_ids.append(ccur.lastrowid)
        conn.execute(
            "UPDATE sources SET duration_secs = COALESCE(duration_secs, ?),"
            " status = 'ready' WHERE source_id = ?",
            (int(result.audio_duration_secs), source_id),
        )
        conn.commit()

        _finalize_display_title(conn, settings, source_id)
        logger.info("podcast source %s ready: %d chunks", source_id, len(chunks))
        return IngestOutcome(source_id, "ready", len(chunks))
    except Exception:
        conn.rollback()
        if inserted_vector_ids:
            placeholders = ",".join("?" * len(inserted_vector_ids))
            conn.execute(
                f"DELETE FROM chunk_vectors WHERE chunk_id IN ({placeholders})",
                inserted_vector_ids,
            )
        conn.execute(
            "UPDATE sources SET status = 'failed' WHERE source_id = ?",
            (source_id,),
        )
        conn.commit()
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _record_vision_usage(conn: sqlite3.Connection):
    from ..rag.chat import _record_llm_usage

    def record(model: str, usage: object) -> None:
        _record_llm_usage(conn, model, usage)

    return record


def _add_visual_chunks(
    conn: sqlite3.Connection,
    embedder: Embedder,
    source_id: int,
    title: str,
    visual_chunks: list,
) -> int:
    """Insert visual chunks (modality=visual) alongside existing audio ones.
    AI-read frame text is source content, so it IS embedded (unlike AI chat
    answers, rule 12); it describes the video, not a generated reply."""
    if not visual_chunks:
        return 0
    vectors = embedder.embed_chunks(title, [c.text for c in visual_chunks])
    ids: list[int] = []
    for vc in visual_chunks:
        cur = conn.execute(
            "INSERT INTO chunks (source_id, modality, text, start_sec, end_sec)"
            " VALUES (?, 'visual', ?, ?, ?)",
            (source_id, vc.text, vc.start_sec, vc.end_sec),
        )
        ids.append(cur.lastrowid)
    for cid, vec in zip(ids, vectors):
        conn.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
            (cid, vector_to_blob(vec)),
        )
    conn.commit()
    return len(ids)


def _ingest_image_post(
    conn, settings, embedder, source_id, title, media, workdir,
    inserted_vector_ids: list,
) -> int:
    """Ingest an IG image/carousel post (rule 1: type=image). The caption
    becomes article chunks and the images are read via the vision path; both
    are source content and ARE embedded (rule 12 bars only AI-generated
    answers). Raises if nothing at all could be extracted."""
    from ..rag import vision

    total = 0
    if media.caption:
        cap_chunks = chunk_transcript([TranscriptSegment(
            text=s2twp.convert(media.caption), start_sec=0.0, end_sec=0.0)])
        vectors = embedder.embed_chunks(title, [c.text for c in cap_chunks])
        ids: list[int] = []
        for c in cap_chunks:
            cur = conn.execute(
                "INSERT INTO chunks (source_id, modality, text, start_sec, end_sec)"
                " VALUES (?, 'article', ?, ?, ?)",
                (source_id, c.text, c.start_sec, c.end_sec),
            )
            ids.append(cur.lastrowid)
        for cid, vec in zip(ids, vectors):
            conn.execute(
                "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                (cid, vector_to_blob(vec)),
            )
            inserted_vector_ids.append(cid)
        total += len(ids)

    if settings.anthropic_api_key:
        try:
            vchunks = vision.analyze_images(
                media.image_paths, workdir, settings,
                _record_vision_usage(conn))
            total += _add_visual_chunks(conn, embedder, source_id, title, vchunks)
        except Exception as exc:  # noqa: BLE001 — supplementary, never fatal
            logger.warning("image vision failed for source %s: %s", source_id, exc)
    else:
        logger.info("image vision skipped (no ANTHROPIC_API_KEY)")

    if total == 0:
        raise RuntimeError(
            "圖片貼文無可擷取內容:無貼文文字,且未設定 ANTHROPIC_API_KEY 讀圖")

    covers = settings.data_dir / "thumbnails"
    covers.mkdir(parents=True, exist_ok=True)
    vision._to_jpeg(media.image_paths[0], covers / f"{source_id}.jpg")
    return total


def _ingest_silent_video(
    conn, settings, embedder, source_id, title, media, workdir,
) -> int:
    """Ingest a silent video (no audio stream) via vision on its frames
    (rule 3). Reuses the already-downloaded file — no re-download. Raises if
    the frames yield nothing so the source isn't left empty."""
    from ..rag import vision

    if not settings.anthropic_api_key:
        raise RuntimeError("無聲影片需要 ANTHROPIC_API_KEY 讀取畫面內容")
    frames = vision.extract_keyframes(media.audio_path, workdir / "frames")
    if not frames:
        raise RuntimeError("無聲影片無法擷取任何畫面")
    vchunks = vision.analyze_frames(frames, settings, _record_vision_usage(conn))
    n = _add_visual_chunks(conn, embedder, source_id, title, vchunks)
    if n == 0:
        raise RuntimeError("無聲影片畫面無可擷取內容")
    covers = settings.data_dir / "thumbnails"
    covers.mkdir(parents=True, exist_ok=True)
    vision.save_cover_thumbnail(frames, covers / f"{source_id}.jpg")
    return n


def _maybe_run_vision(
    conn, settings, embedder, source_id, norm, title,
    segments, duration_secs, workdir,
) -> None:
    from ..rag import vision

    if not vision.should_run_vision(False, segments, duration_secs):
        return
    if not settings.anthropic_api_key:
        logger.info("vision skipped (no ANTHROPIC_API_KEY)")
        return
    run_vision(conn, settings, embedder, source_id, norm, title, workdir)


def run_vision(
    conn, settings, embedder, source_id, norm, title, workdir,
    purge_existing: bool = False,
) -> int:
    """Download the full video, extract keyframes, read them, store visual
    chunks, keep a cover thumbnail. Returns the visual-chunk count. Used by
    the inline fallback and the PWA reanalyze button.

    purge_existing drops prior visual chunks — but only *after* the new read
    succeeds, so a transient download/read failure never loses existing
    visual content (review #6)."""
    from ..rag import vision
    from . import videofile

    video_path = videofile.download_video(
        norm, workdir, settings.max_video_duration_secs
    )
    frames = vision.extract_keyframes(video_path, workdir / "frames")
    if not frames:
        return 0
    chunks = vision.analyze_frames(frames, settings, _record_vision_usage(conn))
    if purge_existing:
        _purge_visual_chunks(conn, source_id)
    n = _add_visual_chunks(conn, embedder, source_id, title, chunks)
    # Keep only a cover thumbnail; frames are removed with the workdir.
    covers = settings.data_dir / "thumbnails"
    vision.save_cover_thumbnail(frames, covers / f"{source_id}.jpg")
    logger.info("vision added %d visual chunks to source %s", n, source_id)
    return n


def reanalyze_vision(
    source_id: int, settings: Settings, conn: sqlite3.Connection,
    embedder: Embedder | None = None,
) -> int:
    """PWA 兜底 (rule 3): force a vision pass on an existing source, merging
    visual chunks without touching audio chunks. Removes prior visual chunks
    first so a repeat press doesn't duplicate them."""
    src = conn.execute(
        "SELECT platform, video_id, url_normalized, url_original, title"
        " FROM sources WHERE source_id = ?", (source_id,),
    ).fetchone()
    if src is None:
        raise ValueError(f"source {source_id} 不存在")
    if embedder is None:
        embedder = Embedder(resolve_embedding_model(conn, settings))
    db.ensure_embedding_model(conn, embedder.model_name, embedder.dim)

    # Prefer the canonical URL (no network re-expansion of short links).
    norm = classify(src["url_normalized"] or src["url_original"])
    safe_key = norm.video_id.replace("/", "_").replace(":", "_")
    workdir = settings.data_dir / "tmp" / f"vision_{norm.platform}_{safe_key}"
    try:
        # purge_existing=True: old visual chunks are removed only after the
        # new read succeeds, so a failed reanalyze keeps the prior content.
        return run_vision(conn, settings, embedder, source_id, norm,
                          src["title"] or norm.video_id, workdir,
                          purge_existing=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _purge_visual_chunks(conn: sqlite3.Connection, source_id: int) -> None:
    old = [r["chunk_id"] for r in conn.execute(
        "SELECT chunk_id FROM chunks WHERE source_id = ? AND modality = 'visual'",
        (source_id,))]
    if old:
        ph = ",".join("?" * len(old))
        conn.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({ph})", old)
        conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({ph})", old)
        conn.commit()
