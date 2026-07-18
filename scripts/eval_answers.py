"""Answer-quality eval (EVAL-AND-OPENSOURCE-PLAN gap 2): LLM-as-judge.

Runs a golden Q&A set against the fixture corpus through the *real* RAG chain
(app.rag.chat.answer), then a Haiku judge scores each answer on CLAUDE.md
rule 13 / 13-1: faithfulness, citation completeness, claim attribution. The
"should refuse" case is checked directly (no fabrication when the library has
nothing).

Unlike eval_retrieval (offline, free), this SPENDS API: each question = one
answer call + one judge call. Keep the golden set small. Needs ANTHROPIC_API_KEY
(read from the project data/.env).

Run:  python scripts/eval_answers.py [--budget] [--limit N]
Appends a summary to scripts/eval_corpus/answer_history.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent / "eval_corpus"
DOCS_DIR = CORPUS_DIR / "docs"
QA = CORPUS_DIR / "qa_golden.jsonl"
HISTORY = CORPUS_DIR / "answer_history.jsonl"

JUDGE_MODEL = "claude-haiku-4-5"
JUDGE_PROMPT = """你是嚴格的 RAG 回答評審。以下有「檢索到的來源片段」與「助理回答」。\
只依據來源片段,逐條判斷回答是否符合規則,給 true/false:
1. faithful:回答是否只根據來源片段,未加入來源沒有的外部事實或杜撰。
2. citation:回答結尾是否有「出處」清單,且每條至少含標題與發布日期。
3. attribution:引用來源時是否用「作者主張/影片提到」等歸屬表述,而非當成客觀事實斷言。
只輸出 JSON,不要多餘文字:{"faithful":bool,"citation":bool,"attribution":bool,"reason":"一句話"}"""


def _bootstrap_env() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Answers + judge need the real key; read it from the project .env, then
    # point the DB at a throwaway dir (load_dotenv won't clobber os.environ).
    proj_env = Path(__file__).resolve().parent.parent / "data" / ".env"
    if proj_env.exists():
        for line in proj_env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and "=" in line:
                os.environ.setdefault("ANTHROPIC_API_KEY",
                                      line.split("=", 1)[1].strip())
    tmp = Path(tempfile.mkdtemp(prefix="vidrag_ans_"))
    os.environ["VIDRAG_DATA_DIR"] = str(tmp)
    return tmp


def _judge(client, context: str, answer: str) -> dict:
    resp = client.messages.create(
        model=JUDGE_MODEL, max_tokens=400,
        system=JUDGE_PROMPT,
        messages=[{"role": "user",
                   "content": f"<來源片段>\n{context}\n</來源片段>\n\n"
                              f"<回答>\n{answer}\n</回答>"}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Haiku sometimes wraps JSON in prose/fences — extract the object.
    start, end = raw.find("{"), raw.rfind("}")
    return json.loads(raw[start:end + 1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", action="store_true",
                        help="use Haiku for answers (cheaper) instead of Sonnet")
    parser.add_argument("--limit", type=int, default=0, help="first N questions")
    args = parser.parse_args()

    tmp = _bootstrap_env()

    import anthropic

    from app import db
    from app.config import load_settings
    from app.ingest.pipeline import ingest_text
    from app.rag.chat import answer, new_session_id
    from app.rag.embedder import Embedder

    settings = load_settings(require_keys=False)
    if not settings.anthropic_api_key:
        raise SystemExit("需要 ANTHROPIC_API_KEY(在 data/.env)才能跑回答品質評測")
    if args.budget:
        object.__setattr__(settings, "chat_model_mode", "budget")

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    embedder = Embedder(settings.embedding_model)
    for path in sorted(DOCS_DIR.glob("*.txt")):
        ingest_text(path.read_text(encoding="utf-8"), settings, conn,
                    embedder=embedder, title=path.stem)

    qa = [json.loads(ln) for ln in QA.read_text(encoding="utf-8").splitlines()
          if ln.strip()]
    if args.limit:
        qa = qa[:args.limit]

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    rubric_pass = {"faithful": 0, "citation": 0, "attribution": 0}
    rubric_n = 0
    refuse_ok = refuse_n = 0
    rows = []

    for item in qa:
        res = answer(conn, settings, embedder, item["question"], new_session_id())
        ans = res.answer

        if item["kind"] == "refuse":
            refuse_n += 1
            # A correct refusal cites nothing and says the library has no match.
            ok = (not res.cited_chunks) or ("沒有" in ans and "相關" in ans)
            refuse_ok += 1 if ok else 0
            rows.append({"id": item["id"], "kind": "refuse", "ok": ok,
                         "answer": ans[:80]})
            continue

        context = "\n\n".join(
            f"[{c.display_title or c.title}] {c.text}"
            for _, c in res.cited_chunks)
        try:
            verdict = _judge(client, context, ans)
        except Exception as exc:  # noqa: BLE001 — judge parse/api failure is data
            rows.append({"id": item["id"], "error": str(exc)[:80]})
            continue
        rubric_n += 1
        for k in rubric_pass:
            if verdict.get(k) is True:
                rubric_pass[k] += 1
        rows.append({"id": item["id"], "kind": item["kind"], **{
            k: verdict.get(k) for k in rubric_pass}, "reason": verdict.get("reason")})

    # --- report ---
    print(f"\nvidrag answer-quality eval — {len(qa)} questions "
          f"(model={settings.chat_model}, judge={JUDGE_MODEL})")
    print("-" * 60)
    for r in rows:
        if r.get("error"):
            print(f"  {r['id']}  ERROR {r['error']}")
        elif r["kind"] == "refuse":
            print(f"  {r['id']}  refuse: {'OK' if r['ok'] else 'FAIL'}")
        else:
            flags = " ".join(f"{k}={'Y' if r[k] else 'n'}"
                             for k in ("faithful", "citation", "attribution"))
            print(f"  {r['id']}  {flags}  — {r.get('reason', '')[:50]}")
    print("-" * 60)
    if rubric_n:
        for k, v in rubric_pass.items():
            print(f"  {k:<12} {v}/{rubric_n}  ({v / rubric_n:.0%})")
    if refuse_n:
        print(f"  refuse-correct {refuse_ok}/{refuse_n}")

    record = {"model": settings.chat_model, "judge": JUDGE_MODEL,
              "n": rubric_n, "refuse_n": refuse_n, "refuse_ok": refuse_ok,
              "rubric_pass": rubric_pass}
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\nappended to {HISTORY.relative_to(CORPUS_DIR.parent.parent)}")

    conn.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
