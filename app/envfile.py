"""Read/update data/.env in place (admin key updates, wizard, token reset).

Values are never logged. Lines that aren't KEY=VALUE (comments, blanks)
are preserved verbatim.
"""

from __future__ import annotations

import os
from pathlib import Path


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    # A value carrying a newline could smuggle extra KEY=VALUE lines into
    # .env (e.g. via a pasted API key) — reject outright (audit 2026-07-15).
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key} 的值不可包含換行")
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    # Keep the running process in sync — Settings reads os.environ.
    for key, value in updates.items():
        os.environ[key] = value


def mask_key(value: str) -> str:
    if not value:
        return "(未設定)"
    return f"****{value[-4:]}" if len(value) >= 8 else "****"
