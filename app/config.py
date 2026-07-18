"""Central configuration: loads data/.env, exposes typed settings.

All persistent state lives under DATA_DIR (CLAUDE.md rule 18).
API keys must never appear in code or logs (KICKOFF environment rules).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("VIDRAG_DATA_DIR", PROJECT_ROOT / "data"))

APP_VERSION = "0.3.0"

# Model IDs pinned by CLAUDE.md rule 6 (accuracy over cost).
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5"

# Transcription: full whisper-large-v3, not turbo (CLAUDE.md rule 3).
GROQ_WHISPER_MODEL = "whisper-large-v3"

# Embedding candidates for the Phase 1 A/B eval; winner is locked into meta.
EMBEDDING_CANDIDATES = {
    "minilm": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "bge-small-zh": "BAAI/bge-small-zh-v1.5",
}
# Default for fresh databases; once meta records a model, meta wins and a
# mismatch refuses startup (CLAUDE.md rule 14).
# Phase 1 A/B eval result (scripts/ab_eval.py, 2026-07-07):
#   bge-small-zh-v1.5 hit@1 19/20, hit@3 20/20 — beat MiniLM (15/20, 18/20).
DEFAULT_EMBEDDING_MODEL = EMBEDDING_CANDIDATES["bge-small-zh"]


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    groq_api_key: str
    anthropic_api_key: str
    app_token: str
    # Dedicated bearer token for the MCP endpoint (Phase 5). Separate from
    # APP_TOKEN: MCP stays disabled (403) until this is generated from the
    # admin page, and resetting one credential never breaks the other.
    mcp_token: str
    monthly_budget_usd: float
    max_video_duration_secs: int
    chat_model_mode: str  # "accurate" | "budget"
    # Cloud-render fallback for JS-only pages (rule 2 amendment 2026-07-15,
    # user-approved): off by default; toggled from the admin page.
    jina_fallback: bool = False
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    # Name of the encrypted rclone remote for off-device backup replication
    # (rule 16). Empty = offsite backup not configured; replication no-ops.
    backup_remote: str = ""

    @property
    def vocabulary_path(self) -> Path:
        return self.data_dir / "vocabulary.txt"

    @property
    def ig_cookies_path(self) -> Path:
        # Instagram login cookies (Netscape cookies.txt) for the IG adapter.
        # A credential — lives under data/ (gitignored), never logged.
        return self.data_dir / "ig_cookies.txt"

    @property
    def rclone_conf_path(self) -> Path:
        # Lives under data/ so it persists with the portable unit (rule 20)
        # and never enters git (data/ is gitignored) — it holds NAS creds.
        return self.data_dir / "rclone.conf"

    @property
    def chat_model(self) -> str:
        return SONNET_MODEL if self.chat_model_mode == "accurate" else HAIKU_MODEL

    @property
    def cheap_model(self) -> str:
        return HAIKU_MODEL


def _require(name: str, value: str, hint: str) -> str:
    if not value:
        raise ConfigError(f"缺少必要設定 {name}:{hint}")
    return value


def load_settings(*, require_keys: bool = True) -> Settings:
    """Load settings from data/.env.

    require_keys=False lets key-free stages (normalize, db init, chunking)
    run without API keys, e.g. in tests and verification scripts.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv(DATA_DIR / ".env")

    groq_key = os.environ.get("GROQ_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    app_token = os.environ.get("APP_TOKEN", "")
    if require_keys:
        groq_key = _require(
            "GROQ_API_KEY", groq_key, "請在 data/.env 填入(console.groq.com 申請)"
        )

    mode = os.environ.get("CHAT_MODEL_MODE", "accurate").strip().lower()
    if mode not in ("accurate", "budget"):
        raise ConfigError(
            f"CHAT_MODEL_MODE 必須是 accurate 或 budget,目前為 {mode!r}"
        )

    try:
        budget = float(os.environ.get("MONTHLY_BUDGET_USD", "5"))
        max_duration = int(os.environ.get("MAX_VIDEO_DURATION_SECS", "10800"))
    except ValueError as exc:
        raise ConfigError(f"數值設定格式錯誤:{exc}") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ConfigError(f"MONTHLY_BUDGET_USD 必須是非負數,目前為 {budget!r}")
    if max_duration <= 0:
        raise ConfigError(f"MAX_VIDEO_DURATION_SECS 必須為正整數,目前為 {max_duration}")

    return Settings(
        data_dir=DATA_DIR,
        db_path=DATA_DIR / "vidrag.db",
        groq_api_key=groq_key,
        anthropic_api_key=anthropic_key,
        app_token=app_token,
        mcp_token=os.environ.get("MCP_TOKEN", ""),
        monthly_budget_usd=budget,
        max_video_duration_secs=max_duration,
        chat_model_mode=mode,
        jina_fallback=os.environ.get("JINA_FALLBACK", "0").strip() == "1",
        embedding_model=os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        backup_remote=os.environ.get("BACKUP_REMOTE", "").strip(),
    )
