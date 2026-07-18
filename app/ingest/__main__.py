"""Phase 1 CLI: python -m app.ingest <url> [url ...]"""

from __future__ import annotations

import logging
import sys

from .. import db
from ..config import load_settings
from .pipeline import ingest_url


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python -m app.ingest <url> [url ...]")
        return 2
    # Windows consoles may default to a legacy codepage (cp950); never let
    # output encoding crash the CLI.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    settings = load_settings(require_keys=False)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    exit_code = 0
    for url in argv:
        try:
            outcome = ingest_url(url, settings, conn)
        except Exception as exc:
            print(f"✗ {url}\n  {exc}")
            exit_code = 1
            continue
        if outcome.status == "duplicate":
            print(f"✓ {url}\n  已在庫內(source_id={outcome.source_id}),不重複攝取")
        else:
            print(
                f"✓ {url}\n  入庫完成 source_id={outcome.source_id}"
                f"({outcome.chunk_count} chunks)"
            )
    conn.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
