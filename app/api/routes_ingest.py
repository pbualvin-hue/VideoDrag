"""POST /api/ingest, GET /api/jobs/{id}, /api/videos, /api/search."""

from __future__ import annotations

import json
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..errors import translate
from ..ingest.normalize import NormalizeError, classify
from ..ingest.pipeline import MAX_TEXT_CHARS, text_content_id
from ..rag.summarize import enqueue_summary
from ..textnorm import s2twp
from ..worker import enqueue_ingest, enqueue_ingest_podcast, enqueue_ingest_text
from .deps import get_conn, require_token

router = APIRouter(dependencies=[Depends(require_token)])

# Manual-text guardrails (rule 1 手動文字): a paste is user input — bound it.
# Char cap lives in pipeline (MAX_TEXT_CHARS) so no enqueue path can bypass it.
_BARE_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


class IngestRequest(BaseModel):
    url: str | None = None
    urls: list[str] | None = None
    text: str | None = None    # manual text ingest (no URL)
    title: str | None = None   # optional user title for `text`


def _ingest_text_request(req: IngestRequest, conn: sqlite3.Connection):
    """Queue a manual text paste; instant-return duplicates like the URL path.
    Returns None when the "text" is actually a bare URL — the caller routes
    it through the link pipeline (whitelist/SSRF guards live there). This
    makes `text` a single-field entry point for the iOS shortcut (2026-07-17:
    捷徑同時接收 URL 與文字,一律送 text 欄)."""
    body = req.text.strip()
    if not body:
        raise HTTPException(400, "文字內容為空")
    if len(body) > MAX_TEXT_CHARS:
        raise HTTPException(
            400, f"文字超過 {MAX_TEXT_CHARS:,} 字上限(目前 {len(body):,} 字),"
                 "請分段貼上")
    if _BARE_URL_RE.match(body):
        return None
    if req.title and len(req.title.strip()) > 200:
        raise HTTPException(400, "標題超過 200 字上限")
    content_id = text_content_id(s2twp.convert(body))
    existing = conn.execute(
        "SELECT source_id FROM sources WHERE platform = 'manual'"
        " AND video_id = ? AND status != 'failed'", (content_id,),
    ).fetchone()
    if existing:
        return {"results": [{"status": "duplicate",
                             "source_id": existing["source_id"]}]}
    job_id = enqueue_ingest_text(conn, body, req.title)
    return {"results": [{"status": "queued", "job_id": job_id}]}


@router.post("/ingest")
def ingest(req: IngestRequest, conn: sqlite3.Connection = Depends(get_conn)):
    if req.text is not None and (req.url or req.urls):
        raise HTTPException(400, "text 與 url 不可同時提供")
    if req.text is not None:
        result = _ingest_text_request(req, conn)
        if result is not None:
            return result
        # text 其實是一條裸連結 → 落入下方連結管線(單欄位入口)
        req = IngestRequest(url=req.text.strip())
    urls = [u.strip() for u in (req.urls or ([req.url] if req.url else []))
            if u and u.strip()]
    if not urls:
        raise HTTPException(400, "缺少 url")
    results = []
    for url in urls:
        try:
            norm = classify(url)
        except NormalizeError as exc:
            results.append({"url": url, "status": "rejected", "reason": str(exc)})
            continue
        existing = conn.execute(
            "SELECT source_id, status FROM sources WHERE platform = ?"
            " AND video_id = ? AND status != 'failed'",
            (norm.platform, norm.video_id),
        ).fetchone()
        if existing:
            results.append({"url": url, "status": "duplicate",
                            "source_id": existing["source_id"]})
            continue
        job_id = enqueue_ingest(conn, url)
        results.append({"url": url, "status": "queued", "job_id": job_id})
    return {"results": results}


class PodcastFeedRequest(BaseModel):
    url: str


@router.post("/podcast/feed")
def podcast_feed(req: PodcastFeedRequest,
                 conn: sqlite3.Connection = Depends(get_conn)):
    """貼 RSS → 回集數清單供挑選(rule 1 podcast, 2026-07-17)。"""
    from ..errors import translate
    from ..ingest import podcast

    url = req.url.strip()
    if not url:
        raise HTTPException(400, "缺少 RSS 網址")
    try:
        feed = podcast.fetch_feed(url)
    except Exception as exc:  # noqa: BLE001 — 人話翻譯(rule 18)
        human = translate(str(exc))
        raise HTTPException(422, f"{human.message}({str(exc)[:120]})")
    for ep in feed["episodes"]:
        row = conn.execute(
            "SELECT 1 FROM sources WHERE platform = 'podcast'"
            " AND video_id = ? AND status != 'failed'",
            (podcast.episode_id(ep["guid"]),),
        ).fetchone()
        ep["in_library"] = bool(row)
    return feed


class PodcastIngestRequest(BaseModel):
    feed_title: str = ""
    episodes: list[dict]


@router.post("/podcast/ingest")
def podcast_ingest(req: PodcastIngestRequest,
                   conn: sqlite3.Connection = Depends(get_conn)):
    """把選取的集數排入轉錄佇列(每次至多 10 集,防手滑排爆額度)。"""
    if not req.episodes:
        raise HTTPException(400, "沒有選取任何集數")
    if len(req.episodes) > 10:
        raise HTTPException(400, "一次最多入庫 10 集")
    results = []
    for ep in req.episodes:
        if not ep.get("guid") or not ep.get("audio_url"):
            raise HTTPException(400, "集數資料不完整(缺 guid/audio_url)")
        # 數值欄位收斂型別(review W2):client 送字串 "123" 會在 pipeline
        # 的時長比較炸 TypeError → 反覆重試到燒完 max_retries
        raw_dur = ep.get("duration_secs")
        duration = (int(raw_dur) if isinstance(raw_dur, int)
                    or (isinstance(raw_dur, str) and raw_dur.isdigit())
                    else None)
        episode = {
            "guid": str(ep["guid"])[:500],
            "title": str(ep.get("title") or "(無標題)")[:200],
            "audio_url": str(ep["audio_url"])[:1000],
            "link": (str(ep["link"])[:1000] if ep.get("link") else None),
            "published_at": (str(ep["published_at"])[:25]
                             if ep.get("published_at") else None),
            "duration_secs": duration,
            "feed_title": str(req.feed_title or "")[:100],
        }
        job_id = enqueue_ingest_podcast(conn, episode)
        results.append({"guid": episode["guid"], "status": "queued",
                        "job_id": job_id})
    return {"results": results}


@router.get("/jobs/{job_id}")
def job_status(job_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    job = conn.execute(
        "SELECT job_id, kind, status, source_id, error, created_at, updated_at"
        " FROM jobs WHERE job_id = ?", (job_id,),
    ).fetchone()
    if job is None:
        raise HTTPException(404, "job 不存在")
    out = {k: job[k] for k in ("job_id", "kind", "status", "source_id",
                               "created_at", "updated_at")}
    if job["status"] == "failed" and job["error"]:
        human = translate(job["error"])
        out["human_error"] = {"category": human.category,
                              "message": human.message,
                              "action": human.action,
                              "action_kind": human.action_kind}
    if job["status"] == "pending":
        ahead = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','processing')"
            " AND job_id < ?", (job_id,),
        ).fetchone()[0]
        out["queue_position"] = ahead
    return out


@router.get("/videos")
def list_videos(conn: sqlite3.Connection = Depends(get_conn)):
    # 研究庫的週分組標頭依賴「回傳順序 = ingested_at 遞減」——排序必須以
    # ingested_at 為主鍵,否則前端會出現重複的週標頭(review S3)。
    sources = [dict(r) for r in conn.execute(
        """SELECT source_id, type, platform, video_id, title, display_title,
                  url_normalized, published_at, duration_secs, ingested_at,
                  status, summary, collection_id
           FROM sources ORDER BY ingested_at DESC, source_id DESC"""
    )]
    for s in sources:
        if s["status"] == "failed":
            row = conn.execute(
                "SELECT error FROM jobs WHERE source_id = ? AND error IS NOT NULL"
                " ORDER BY job_id DESC LIMIT 1", (s["source_id"],),
            ).fetchone()
            human = translate(row["error"] if row else "")
            s["human_error"] = {"category": human.category,
                                "message": human.message,
                                "action": human.action,
                                "action_kind": human.action_kind}
    # Queued items that don't have a source row yet (still pending fetch).
    queued = []
    for r in conn.execute(
        "SELECT job_id, payload, status FROM jobs"
        " WHERE kind IN ('ingest', 'ingest_text', 'ingest_podcast')"
        " AND status IN ('pending', 'processing') AND source_id IS NULL"
        " ORDER BY job_id"
    ):
        payload = json.loads(r["payload"])
        ep = payload.get("episode") or {}
        queued.append({"job_id": r["job_id"], "status": r["status"],
                       "url": payload.get("url"),
                       "preview": (payload.get("text")
                                   or ("🎙 " + ep["title"] if ep.get("title") else "")
                                   )[:40]})
    for i, q in enumerate(qi for qi in queued if qi["status"] == "pending"):
        q["queue_position"] = i
    return {"sources": sources, "queued": queued}


@router.get("/videos/{source_id}/thumbnail")
def video_thumbnail(source_id: int):
    from fastapi.responses import FileResponse

    from ..config import load_settings
    from ..thumbnails import thumbnail_path

    path = thumbnail_path(load_settings(require_keys=False), source_id)
    if not path.exists():
        raise HTTPException(404, "無縮圖")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/videos/{source_id}")
def video_detail(source_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    src = conn.execute(
        "SELECT * FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    if src is None:
        raise HTTPException(404, "影片不存在")
    chunks = [dict(r) for r in conn.execute(
        "SELECT chunk_id, modality, text, start_sec, end_sec FROM chunks"
        " WHERE source_id = ? ORDER BY start_sec", (source_id,),
    )]
    # Lazy summary trigger (rule 10b): opening the detail view.
    if src["status"] in ("ready", "enriched"):
        enqueue_summary(conn, source_id)
    return {"source": dict(src), "chunks": chunks}


class TitleRequest(BaseModel):
    display_title: str


@router.patch("/videos/{source_id}/title")
def rename_display_title(source_id: int, req: TitleRequest,
                         conn: sqlite3.Connection = Depends(get_conn)):
    """User rename of the AI topic name (回饋批次 2026-07-17)。原始標題
    (sources.title) 不動——資訊源歸屬照舊,只改顯示名。"""
    name = " ".join(req.display_title.split())
    if not name:
        raise HTTPException(400, "標題不可為空")
    if len(name) > 40:
        raise HTTPException(400, "標題超過 40 字上限")
    cur = conn.execute(
        "UPDATE sources SET display_title = ? WHERE source_id = ?",
        (name, source_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "來源不存在")
    return {"source_id": source_id, "display_title": name}


@router.post("/videos/{source_id}/reanalyze")
def reanalyze(source_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """PWA「重新分析畫面」: force a vision pass, merging visual chunks."""
    from ..config import load_settings
    from ..ingest.pipeline import reanalyze_vision

    settings = load_settings(require_keys=False)
    if not settings.anthropic_api_key:
        raise HTTPException(503, "尚未設定 ANTHROPIC_API_KEY,無法分析畫面")
    try:
        n = reanalyze_vision(source_id, settings, conn)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        human = translate(str(exc))
        raise HTTPException(500, human.message)
    return {"source_id": source_id, "visual_chunks_added": n}


@router.delete("/videos/{source_id}")
def delete_video(source_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    src = conn.execute(
        "SELECT source_id FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    if src is None:
        raise HTTPException(404, "影片不存在")
    # Remove the source's job history outright (小修 2026-07-17):terminal
    # rows have no residual value, they only accumulate — and the verbatim
    # manual-text payload must not outlive the source (audit 2026-07-14).
    # A worker mid-run on one of these rows finishes harmlessly: its later
    # UPDATE just matches zero rows.
    conn.execute("DELETE FROM jobs WHERE source_id = ?", (source_id,))
    db.delete_source(conn, source_id)
    # Cascade to the cover thumbnail on disk (rule 12-2).
    from ..config import load_settings
    from ..thumbnails import thumbnail_path
    thumbnail_path(load_settings(require_keys=False), source_id).unlink(missing_ok=True)
    return {"deleted": source_id}


@router.get("/search")
def quick_search(q: str, conn: sqlite3.Connection = Depends(get_conn)):
    """影片庫搜尋框:FTS5 直查,不經 AI(UX.md 分頁 2)。"""
    from ..rag.chat import _fts_query

    fts = _fts_query(q)
    if not fts:
        return {"hits": []}
    try:
        rows = conn.execute(
            """SELECT c.chunk_id, c.source_id, c.start_sec,
                      snippet(chunks_fts, 0, '【', '】', '…', 20) AS snippet,
                      s.title, s.display_title, s.url_normalized, s.published_at
               FROM chunks_fts f
               JOIN chunks c ON c.chunk_id = f.rowid
               JOIN sources s ON s.source_id = c.source_id
               WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 20""",
            (fts,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"hits": []}
    return {"hits": [dict(r) for r in rows]}
