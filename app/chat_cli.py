"""Phase 2 CLI: python -m app.chat_cli [options] <question>

Sessions:
  (default)        continue the most recent session, or start one
  --new            start a new session
  --session ID     continue a specific session
  --sessions       list sessions and exit
Scope / mode:
  --video ID       restrict retrieval to one source
  --verify         cross-check the answer against web search (extra cost)
"""

from __future__ import annotations

import argparse
import sys

from . import db
from .config import load_settings
from .rag import chat
from .rag.embedder import Embedder
from .rag.summarize import run_pending_summaries


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="python -m app.chat_cli")
    parser.add_argument("question", nargs="*", help="要問的問題")
    parser.add_argument("--new", action="store_true", help="開新對話")
    parser.add_argument("--session", help="續聊指定 session")
    parser.add_argument("--sessions", action="store_true", help="列出對話")
    parser.add_argument("--video", type=int, help="限定某支影片(source_id)")
    parser.add_argument("--verify", action="store_true", help="啟用查證模式")
    parser.add_argument("--stats", action="store_true", help="顯示當月費用")
    args = parser.parse_args(argv)

    settings = load_settings(require_keys=False)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    if args.stats:
        month = db.month_cost_usd(conn)
        print(f"本月 API 費用:US${month:.4f} / 預算 US${settings.monthly_budget_usd:.2f}")
        for r in conn.execute(
            "SELECT provider, model, COUNT(*) n, SUM(input_tokens) ti,"
            " SUM(output_tokens) to_, ROUND(SUM(cost_usd), 4) c FROM api_usage"
            " WHERE ts LIKE strftime('%Y-%m', 'now') || '%'"
            " GROUP BY provider, model ORDER BY c DESC"
        ):
            print(f"  {r['provider']}/{r['model']}: {r['n']} 次,"
                  f" in {r['ti'] or 0} / out {r['to_'] or 0} tokens, US${r['c']}")
        if month > settings.monthly_budget_usd:
            print("⚠️ 已超過本月預算")
        return 0

    if args.sessions:
        rows = chat.list_sessions(conn)
        if not rows:
            print("尚無任何對話。")
        for r in rows:
            title = (r["first_question"] or "(無標題)")[:40]
            print(f"{r['session_id']}  [{r['last_at']}] ({r['n']} 則) {title}")
        return 0

    question = " ".join(args.question).strip()
    if not question:
        parser.print_help()
        return 2
    if not settings.anthropic_api_key:
        print("缺少 ANTHROPIC_API_KEY:請在 data/.env 填入後再使用對話功能。")
        return 1

    if args.session:
        session_id = args.session
    elif args.new:
        session_id = chat.new_session_id()
    else:
        session_id = chat.latest_session_id(conn) or chat.new_session_id()

    embedder = Embedder(
        db.get_meta(conn, "embedding_model") or settings.embedding_model
    )
    db.ensure_embedding_model(conn, embedder.model_name, embedder.dim)

    result = chat.answer(
        conn, settings, embedder, question, session_id,
        source_id=args.video, verify=args.verify,
    )

    print(f"[session {result.session_id}]\n")
    print(result.answer)
    if result.cited_chunks:
        print("\n— 引用片段 —")
        for label, c in result.cited_chunks:
            span = (f"{chat.format_timestamp(c.start_sec)}-"
                    f"{chat.format_timestamp(c.end_sec)}")
            published = (c.published_at or "未知")[:10]
            print(f"  S{label}. {c.title}({span},發布 {published})")
    if result.verification:
        print("\n— 查證結果 —")
        print(result.verification)
    if result.budget_warning:
        print(f"\n{result.budget_warning}")

    # Lazy summaries run after the answer is printed — never blocking it.
    run_pending_summaries(conn, settings)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
