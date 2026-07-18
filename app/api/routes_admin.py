"""/api/admin/* + /api/setup — everything the 管理頁 needs (rule 15:
zero-command operations)."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import backup, db
from ..config import APP_VERSION, Settings
from ..envfile import mask_key, update_env_file
from .deps import get_conn, get_settings, require_token

router = APIRouter(dependencies=[Depends(require_token)])
setup_router = APIRouter()  # setup endpoints run before a token exists


# ---------- health ----------

@router.get("/admin/health")
def health(
    conn: sqlite3.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
):
    usage = shutil.disk_usage(settings.data_dir)
    queue_len = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'processing')"
    ).fetchone()[0]
    # Count failed SOURCES (what the 研究庫 actually shows), not failed job
    # rows — retries/history inflate the job count (使用者回饋 2026-07-14)。
    failed = conn.execute(
        "SELECT COUNT(*) FROM sources WHERE status = 'failed'"
    ).fetchone()[0]
    return {
        "version": APP_VERSION,
        "disk": {"total_gb": round(usage.total / 2**30, 1),
                 "free_gb": round(usage.free / 2**30, 1),
                 "used_pct": round(100 * usage.used / usage.total, 1)},
        "queue_length": queue_len,
        "failed_sources": failed,
        "last_backup_at": db.get_meta(conn, "last_backup_at"),
        "last_integrity_at": db.get_meta(conn, "last_integrity_at"),
        "integrity_ok": db.get_meta(conn, "last_integrity_ok") != "0",
        "source_count": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        "backup_offsite": False,  # rclone destination not configured yet
        "jina_fallback": settings.jina_fallback,
    }


# ---------- backups ----------

@router.get("/admin/backups")
def list_backups_endpoint(settings: Settings = Depends(get_settings)):
    return {"backups": [
        {"name": p.name, "size_mb": round(p.stat().st_size / 2**20, 2)}
        for p in backup.list_backups(settings)
    ]}


@router.post("/admin/backup")
def backup_now(settings: Settings = Depends(get_settings)):
    path = backup.run_backup(settings)
    backup.replicate_offsite(settings)  # no-op until offsite is configured
    return {"created": path.name}


class RestoreRequest(BaseModel):
    name: str


@router.post("/admin/restore")
def restore(req: RestoreRequest, request: Request,
            settings: Settings = Depends(get_settings)):
    worker = request.app.state.worker
    # If a long ingest can't drain in time, don't swap the db under it.
    if not worker.stop():
        worker.start()
        raise HTTPException(409, "攝取進行中,請稍後再還原")
    try:
        backup.restore_backup(settings, req.name)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    finally:
        worker.start()
    ok = backup.run_integrity_check(settings)
    return {"restored": req.name, "integrity_ok": ok}


# ---------- offsite encrypted backup (rule 16) ----------


class OffsiteConfigRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    password: str
    folder: str          # NAS path, e.g. "/vidrag-backups"
    crypt_password: str  # encrypts backups before they leave the Pi


@router.post("/admin/offsite/config")
def offsite_config(req: OffsiteConfigRequest,
                   settings: Settings = Depends(get_settings)):
    """Build the encrypted rclone remotes (sftp base + crypt wrapper) from the
    user's NAS details. rclone obscures the secrets in data/rclone.conf; they
    are never logged here, and rclone stderr is not echoed (it can contain the
    submitted values)."""
    required = (("host", req.host), ("user", req.user),
                ("folder", req.folder), ("crypt_password", req.crypt_password))
    for name, val in required:
        if not val.strip():
            raise HTTPException(400, f"缺少必填欄位:{name}")
    # Reject control chars so a value can't inject a new line/section into
    # rclone.conf (security review 🔵-1).
    for name, val in (*required, ("password", req.password)):
        if any(c in val for c in "\r\n\x00"):
            raise HTTPException(400, f"{name} 含非法字元")
    if not 1 <= req.port <= 65535:
        raise HTTPException(400, "port 需為 1–65535")

    conf = settings.rclone_conf_path
    conf.unlink(missing_ok=True)  # own this file; rebuild both remotes cleanly

    def _create(*args: str) -> None:
        r = subprocess.run(
            ["rclone", "--config", str(conf), "config", "create", *args, "--obscure"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise HTTPException(400, "rclone 設定寫入失敗,請檢查欄位格式")

    _create("nas", "sftp", f"host={req.host.strip()}", f"user={req.user.strip()}",
            f"port={req.port}", f"pass={req.password}")
    _create("nas-crypt", "crypt", f"remote=nas:{req.folder.strip()}",
            f"password={req.crypt_password}")
    update_env_file(settings.data_dir / ".env", {"BACKUP_REMOTE": "nas-crypt"})
    return {"ok": True}


@router.post("/admin/offsite/test")
def offsite_test(settings: Settings = Depends(get_settings)):
    if not settings.backup_remote or not settings.rclone_conf_path.is_file():
        raise HTTPException(400, "尚未設定異地備份")
    r = subprocess.run(
        ["rclone", "--config", str(settings.rclone_conf_path), "lsd",
         f"{settings.backup_remote}:", "--timeout", "20s"],
        capture_output=True, text=True, timeout=40,
    )
    if r.returncode == 0:
        return {"ok": True}
    raise HTTPException(400, backup.rclone_error_message(r.stderr))


@router.get("/admin/offsite/status")
def offsite_status(settings: Settings = Depends(get_settings),
                   conn: sqlite3.Connection = Depends(get_conn)):
    return {
        "configured": bool(settings.backup_remote) and settings.rclone_conf_path.is_file(),
        "last_ok": db.get_meta(conn, "last_offsite_ok"),
        "last_at": db.get_meta(conn, "last_offsite_at"),
        "last_error": db.get_meta(conn, "last_offsite_error"),
    }


# ---------- Instagram cookie (Phase 4-5) ----------


class IgCookieRequest(BaseModel):
    content: str


@router.post("/admin/ig-cookie")
def upload_ig_cookie(req: IgCookieRequest, settings: Settings = Depends(get_settings)):
    """Store the user's Instagram login cookies (Netscape cookies.txt) for the
    IG adapter. The content is a live login session — it is written to data/
    at 0600 and never logged."""
    content = req.content
    if len(content.encode("utf-8")) > 512 * 1024:
        raise HTTPException(400, "cookie 檔過大")
    if "instagram" not in content.lower() or "\t" not in content:
        raise HTTPException(
            400, "這不像 Instagram 的 Netscape cookies.txt(請用「Get cookies.txt LOCALLY」匯出)"
        )
    # Create at 0600 from the start: avoid a world-readable window between
    # write and a later chmod, and never leave a login credential at 0644 on a
    # swallowed chmod error (error-handling rule: no silent failure). umask can
    # only clear bits, so the file is at most 0600.
    fd = os.open(settings.ig_cookies_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True}


@router.get("/admin/ig-cookie/status")
def ig_cookie_status(settings: Settings = Depends(get_settings)):
    p = settings.ig_cookies_path
    if not p.is_file():
        return {"present": False}
    mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
    return {"present": True, "updated_at": mtime.strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---------- vocabulary ----------

@router.get("/admin/vocabulary", response_class=PlainTextResponse)
def get_vocabulary(settings: Settings = Depends(get_settings)):
    path = settings.vocabulary_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


class VocabularyRequest(BaseModel):
    text: str


@router.post("/admin/vocabulary")
def set_vocabulary(req: VocabularyRequest,
                   settings: Settings = Depends(get_settings)):
    settings.vocabulary_path.write_text(req.text, encoding="utf-8")
    return {"saved": True}


# ---------- API keys / budget / token ----------

@router.get("/admin/keys")
def get_keys(settings: Settings = Depends(get_settings)):
    return {"groq": mask_key(settings.groq_api_key),
            "anthropic": mask_key(settings.anthropic_api_key)}


class KeysRequest(BaseModel):
    groq_api_key: str | None = None
    anthropic_api_key: str | None = None


@router.post("/admin/keys")
def set_keys(req: KeysRequest, settings: Settings = Depends(get_settings)):
    updates: dict[str, str] = {}
    if req.groq_api_key:
        updates["GROQ_API_KEY"] = req.groq_api_key.strip()
    if req.anthropic_api_key:
        updates["ANTHROPIC_API_KEY"] = req.anthropic_api_key.strip()
    if not updates:
        raise HTTPException(400, "沒有要更新的金鑰")
    update_env_file(settings.data_dir / ".env", updates)
    return {"updated": sorted(updates)}  # values never echoed


class JinaRequest(BaseModel):
    enabled: bool


@router.post("/admin/jina")
def set_jina(req: JinaRequest, settings: Settings = Depends(get_settings)):
    """Toggle the cloud-render fallback (rule 2 amendment 2026-07-15).
    Off by default; enabling means failed-extract PUBLIC page URLs are sent
    to the third-party Jina Reader for rendering."""
    update_env_file(settings.data_dir / ".env",
                    {"JINA_FALLBACK": "1" if req.enabled else "0"})
    return {"jina_fallback": req.enabled}


class BudgetRequest(BaseModel):
    monthly_budget_usd: float


@router.post("/admin/budget")
def set_budget(req: BudgetRequest, settings: Settings = Depends(get_settings)):
    if req.monthly_budget_usd < 0:
        raise HTTPException(400, "預算不可為負")
    update_env_file(settings.data_dir / ".env",
                    {"MONTHLY_BUDGET_USD": str(req.monthly_budget_usd)})
    return {"monthly_budget_usd": req.monthly_budget_usd}


@router.post("/admin/token/reset")
def reset_token(settings: Settings = Depends(get_settings)):
    token = secrets.token_urlsafe(24)
    update_env_file(settings.data_dir / ".env", {"APP_TOKEN": token})
    return {"app_token": token,
            "warning": "舊捷徑與已登入的 PWA 已失效,請重新安裝捷徑"}


# ---------- MCP: connect Claude Desktop (Phase 5) ----------


def _mcp_desktop_config(url: str, token: str, *, windows: bool) -> str:
    """claude_desktop_config.json snippet for the local mcp-remote bridge.

    Verified against mcp-remote docs (2026-07): the header value goes through
    an env var ("Authorization:${AUTH_HEADER}", no spaces in the arg) because
    Claude Desktop on Windows mangles spaces inside args; Windows also needs
    the `cmd /c` wrapper because npx is a .cmd script that spawn() can't exec.
    --transport http-only: this server is streamable-HTTP only (no SSE).
    """
    args = ["/c", "npx"] if windows else []
    args += ["-y", "mcp-remote", url,
             "--header", "Authorization:${AUTH_HEADER}",
             "--transport", "http-only"]
    config = {"mcpServers": {"vidrag": {
        "command": "cmd" if windows else "npx",
        "args": args,
        "env": {"AUTH_HEADER": f"Bearer {token}"},
    }}}
    return json.dumps(config, indent=2, ensure_ascii=False)


@router.get("/admin/mcp/status")
def mcp_status(settings: Settings = Depends(get_settings)):
    return {"enabled": bool(settings.mcp_token)}


@router.post("/admin/mcp/token/reset")
def mcp_token_reset(request: Request, settings: Settings = Depends(get_settings)):
    """Generate (or rotate) the MCP token and return paste-ready connector
    config. The token is returned once for copy-paste — same pattern as
    APP_TOKEN reset — and is never logged."""
    token = secrets.token_urlsafe(24)
    update_env_file(settings.data_dir / ".env", {"MCP_TOKEN": token})
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    url = f"{proto}://{host}/api/mcp"
    return {
        "mcp_url": url,
        "config_windows": _mcp_desktop_config(url, token, windows=True),
        "config_macos": _mcp_desktop_config(url, token, windows=False),
        "steps": [
            "電腦需先安裝 Node.js LTS(內含 npx):nodejs.org 下載安裝",
            "確認這台電腦已連上 Tailscale(打得開這個 PWA 就代表可以)",
            "開 Claude Desktop → 設定(Settings)→ 開發者(Developer)→"
            "「編輯設定」(Edit Config),會開啟 claude_desktop_config.json",
            "把下方 JSON 貼進檔案(若檔內已有 mcpServers,只把 \"vidrag\" 區塊合併進去)",
            "完全結束 Claude Desktop 再重開(Windows:系統匣圖示右鍵 → Quit,"
            "不是只按視窗的 ✕)",
            "開新對話,問庫內收藏過的內容,Claude 會自動呼叫 vidrag 檢索工具",
        ],
        "warning": "重新產生設定會換新 token,舊的 Claude Desktop 設定會失效,需重貼一次。",
    }


# ---------- iOS shortcut ----------

@router.get("/admin/shortcut")
def shortcut_info(request: Request, settings: Settings = Depends(get_settings)):
    import segno

    # Behind Tailscale Serve the app sees http internally; honour the
    # X-Forwarded-Proto the proxy sets so the shortcut URL is https (the
    # ts.net name only serves HTTPS, so an http:// URL would not connect).
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    ingest_url = f"{proto}://{host}/api/ingest?token={settings.app_token}"
    qr = segno.make(ingest_url)
    return {
        "ingest_url": ingest_url,
        # Shown with its own copy button: users pasted the whole ingest URL
        # as the token on desktop and got locked out (回饋 2026-07-14)。
        "app_token": settings.app_token,
        "qr_svg_data_uri": qr.svg_data_uri(scale=4),
        "steps": [
            "「捷徑」App → 右上「+」建立新捷徑,命名「存到 vidrag」",
            "點捷徑的「詳細資料」(ⓘ) → 開啟「在分享表單中顯示」,"
            "接收類型勾選「URL」與「文字」(最上方會自動出現「從分享表單接收」)",
            "加入動作「取得 URL 內容」:把動作的〔URL〕貼成下方網址"
            "(這是 vidrag 端點,不是影片連結)",
            "展開「顯示更多」:方式選 POST、要求內文選 JSON、"
            "「加入新欄位」鍵填 text、值選變數「捷徑輸入」"
            "(影片連結或選取的文字都放這一格,系統會自動分辨)",
            "(選配)加入動作「顯示通知」:已丟進 vidrag",
            "完成後,分享影片、或長按選取文字 → 分享 → 「存到 vidrag」皆可入庫",
        ],
    }


# ---------- Chrome extension ----------

@router.get("/admin/extension.zip")
def extension_zip(request: Request):
    """Bundle app/extension/ into a ready-to-load Chrome (MV3) unpacked
    extension. The configured base URL is injected into the manifest's
    host_permissions and the options page default, so the download works
    without the user editing files. Like the desktop bookmark, the zip
    carries no APP_TOKEN — the user pastes that into the extension's
    options once (rule 15: PWA-driven, no terminal)."""
    import io
    import re
    import zipfile
    from pathlib import Path

    from fastapi.responses import Response

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    # Host feeds two very different sinks; validate its shape up front so a
    # malformed value fails loudly here (rule 18) instead of (a) corrupting the
    # injected HTML/JSON or (b) producing an invalid manifest Chrome rejects
    # with an opaque error. Accepts hostname / IPv4 [+ optional :port].
    if not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?", host):
        raise HTTPException(400, "無法辨識的主機名,Chrome 擴充改用「電腦書籤」")
    base = f"{proto}://{host}"          # exact fetch target (keeps any :port)
    # MV3 host_permissions match patterns must NOT carry a port — the hostname
    # form already matches every port. A ":port" in the pattern makes the whole
    # manifest invalid and unloadable, so strip it here.
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    match = f"{proto}://{hostname}/*"

    ext_dir = Path(__file__).resolve().parents[1] / "extension"
    if not ext_dir.is_dir():
        raise HTTPException(500, "擴充模板缺失(app/extension/)")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ext_dir.iterdir()):
            if not path.is_file():
                continue
            # Only the manifest/options carry the host placeholder; icons are
            # binary and must be copied byte-for-byte (read_text would choke).
            if path.suffix in (".json", ".html"):
                data = (path.read_text(encoding="utf-8")
                        .replace("__VIDRAG_MATCH__", match)
                        .replace("__VIDRAG_HOST__", base))
                zf.writestr(path.name, data)
            else:
                zf.writestr(path.name, path.read_bytes())

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vidrag-extension.zip"'},
    )


# ---------- yt-dlp update ----------

@router.post("/admin/update/ytdlp")
def update_ytdlp(request: Request):
    worker = request.app.state.worker
    worker.stop()  # 更新中鎖定攝取
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300,
        )
        if proc.returncode != 0:
            raise HTTPException(500, f"更新失敗:{proc.stderr[-500:]}")
        # Read the version from a fresh subprocess — importlib.reload in this
        # long-lived process returns the cached pre-upgrade version (review M3).
        ver = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        new_version = ver.stdout.strip() or "已更新"
    finally:
        worker.start()
    # Caveat: Python's module cache is process-wide, so the running process
    # keeps the old yt_dlp module until the container restarts. The installed
    # version is upgraded (reported above); it takes effect on next restart
    # (compose restart:unless-stopped). We surface this in the response so the
    # admin page can tell the user a restart is pending.
    return {"yt_dlp_version": new_version, "restart_pending": True}


@router.get("/admin/version")
def version_info():
    from yt_dlp.version import __version__ as ytdlp_version
    return {"app_version": APP_VERSION, "yt_dlp_version": ytdlp_version}


# ---------- export ----------

@router.get("/admin/export", response_class=PlainTextResponse)
def export_markdown(
    source_id: int | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    where, params = ("WHERE source_id = ?", [source_id]) if source_id else ("", [])
    sources = conn.execute(
        f"SELECT * FROM sources {where} ORDER BY source_id", params
    ).fetchall()
    if not sources:
        raise HTTPException(404, "沒有可匯出的來源")
    lines: list[str] = ["# vidrag 匯出", ""]
    for s in sources:
        lines += [f"## {s['title'] or s['video_id']}",
                  f"- 平台:{s['platform']}",
                  f"- 連結:{s['url_normalized']}",
                  f"- 發布日期:{(s['published_at'] or '未知')[:10]}", ""]
        if s["summary"]:
            lines += ["### 摘要", s["summary"], ""]
        lines.append("### 逐字稿")
        for c in conn.execute(
            "SELECT text, start_sec, end_sec FROM chunks WHERE source_id = ?"
            " ORDER BY start_sec", (s["source_id"],),
        ):
            start = int(c["start_sec"] or 0)
            stamp = f"{start // 60}:{start % 60:02d}"
            link = f"{s['url_normalized']}&t={start}s" \
                if s["platform"] == "youtube" else s["url_normalized"]
            lines += [f"**[{stamp}]({link})**", c["text"], ""]
        lines.append("---")
    return "\n".join(lines)


# ---------- setup wizard (no token yet) ----------

@setup_router.get("/setup/status")
def setup_status(settings: Settings = Depends(get_settings)):
    return {
        "needs_setup": not settings.app_token,
        "has_groq_key": bool(settings.groq_api_key),
        "has_anthropic_key": bool(settings.anthropic_api_key),
        "budget_usd": settings.monthly_budget_usd,
        "version": APP_VERSION,
    }


class SetupRequest(BaseModel):
    groq_api_key: str | None = None
    anthropic_api_key: str | None = None
    monthly_budget_usd: float | None = None


@setup_router.post("/setup")
def run_setup(req: SetupRequest, settings: Settings = Depends(get_settings)):
    if settings.app_token:
        raise HTTPException(403, "系統已完成設定;請改用管理頁修改")
    updates: dict[str, str] = {}
    if req.groq_api_key:
        updates["GROQ_API_KEY"] = req.groq_api_key.strip()
    if req.anthropic_api_key:
        updates["ANTHROPIC_API_KEY"] = req.anthropic_api_key.strip()
    if req.monthly_budget_usd is not None:
        updates["MONTHLY_BUDGET_USD"] = str(req.monthly_budget_usd)
    token = secrets.token_urlsafe(24)
    updates["APP_TOKEN"] = token
    update_env_file(settings.data_dir / ".env", updates)
    return {"app_token": token}
