"""Error translation layer (CLAUDE.md rule 18 / UX.md 第五節).

Every user-facing error is classified into a category with a plain-language
description and a next action. The raw error string is stored only in
jobs.error for debugging — users never see a stack trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class HumanError:
    category: str
    message: str          # what happened, in plain language
    action: str           # what to press / do next
    action_kind: str      # PWA hook: retry / cookie / billing / remove / update_ytdlp / paste / none


_RULES: list[tuple[str, re.Pattern, HumanError]] = [
    # Must precede ig_cookie_expired: a wall page is a content problem, not a
    # cookie problem. Retrying re-fetches the same wall, so it is permanent.
    ("login_wall",
     re.compile(r"疑似登入牆|無法從此網頁擷取正文內容|正文僅"),
     HumanError("login_wall", "這個頁面是動態載入(表單/商店頁)或需要登入,抓不到正文",
                "開啟該頁面全選複製內文,回研究庫貼上區以文字入庫", "paste")),
    ("ig_cookie_expired",
     re.compile(r"login|cookie|authenticate|rate-?limit reached.*instagram|"
                r"requested content is not available", re.I),
     HumanError("ig_cookie_expired", "IG 需要重新登入才能讀取",
                "到管理頁更新 Instagram cookie", "cookie")),
    ("provider_quota",
     re.compile(r"rate.?limit|429|quota|too many requests", re.I),
     HumanError("provider_quota", "今日轉錄額度已滿,稍後會自動重試",
                "可改天再試,或等系統自動排程", "retry")),
    ("llm_billing",
     re.compile(r"billing|credit balance|payment|insufficient.*(funds|credit)|"
                r"purchase", re.I),
     HumanError("llm_billing", "AI 服務餘額不足",
                "前往 console.anthropic.com 或 console.groq.com 儲值", "billing")),
    # Tightened (小修 2026-07-17): the old broad "unavailable" would also
    # swallow transient "Service Unavailable" — now that this category is
    # permanent (no retry), markers must be definitively terminal.
    ("content_gone",
     # 裸數字限定在 HTTP 狀態語境(review S4):permanent 類別誤中
     # 內文碰巧出現的 404/410 代價太高
     re.compile(r"video unavailable|is private|no longer available|"
                r"has been (removed|deleted)|account.*terminated|video.*not exist|"
                r"http (error )?\s*4(04|10)\b|\b4(04|10) (not found|gone)\b",
                re.I),
     HumanError("content_gone", "這個內容已被下架、刪除或設為私人",
                "可將這筆記錄移除", "remove")),
    ("too_long",
     re.compile(r"duration.*>.*limit|too_long|duration_check", re.I),
     HumanError("too_long", "影片超過 3 小時上限,系統不收錄",
                "無需處理", "none")),
    ("network",
     re.compile(r"timed? ?out|connection|network|dns|unreachable|refused", re.I),
     HumanError("network", "網路連線不穩,抓取中斷",
                "稍後重試", "retry")),
    ("extractor_broken",
     re.compile(r"unable to extract|unsupported url|extractor|signature|"
                r"403: forbidden|http error 403", re.I),
     HumanError("extractor_broken", "平台改版導致抓取失敗",
                "到管理頁一鍵更新 yt-dlp 後重試", "update_ytdlp")),
]

_FALLBACK = HumanError(
    "unknown", "處理時發生預期外的錯誤", "重試一次;若持續失敗請看管理頁健康狀態", "retry"
)


def translate(raw_error: str) -> HumanError:
    """Map a raw provider/library error to a human-readable one."""
    for _, pattern, human in _RULES:
        if pattern.search(raw_error or ""):
            return human
    return _FALLBACK


# Retrying these cannot change the outcome — the page will still demand a
# login / the content is still gone. Burning max_retries just re-hits the
# remote site (content_gone added 小修 2026-07-17: 404/410 重試無意義).
_PERMANENT_CATEGORIES = frozenset({"login_wall", "content_gone"})


def is_permanent(raw_error: str) -> bool:
    """True when a retry is futile, so the worker should fail the job at once."""
    return translate(raw_error).category in _PERMANENT_CATEGORIES
