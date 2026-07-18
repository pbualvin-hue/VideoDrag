"""Retrieval evaluation harness (EVAL-AND-OPENSOURCE-PLAN 缺口 1).

Loads a fixed fixture corpus into a throwaway DB via the *real* ingest
pipeline, runs a golden query set through the *real* hybrid retrieval
(`app.rag.chat.retrieve`), and reports recall@k, MRR, and — the headline
number — which route (vector KNN vs FTS5 BM25) actually surfaced each gold
chunk. Fully offline: fastembed runs locally, no API keys, zero API cost.

Run:  python scripts/eval_retrieval.py
      python scripts/eval_retrieval.py --k 5

Results append to scripts/eval_corpus/eval_history.jsonl. Discipline (per the
plan): after any change to chunking, the embedding model, or the retrieval
merge logic, run this and record the before/after.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent / "eval_corpus"
DOCS_DIR = CORPUS_DIR / "docs"
GOLDEN = CORPUS_DIR / "golden.jsonl"
HISTORY = CORPUS_DIR / "eval_history.jsonl"


def _bootstrap_env() -> Path:
    """Point the app at a fresh temp data dir and strip API keys BEFORE any
    app.* import (config.DATA_DIR is bound at import time; keys gate the paid
    display-title step). Returns the temp data dir."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    tmp = Path(tempfile.mkdtemp(prefix="vidrag_eval_"))
    os.environ["VIDRAG_DATA_DIR"] = str(tmp)
    for key in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "APP_TOKEN"):
        os.environ.pop(key, None)
    return tmp


def load_golden() -> list[dict]:
    items = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def validate_fixture(golden: list[dict], convert) -> dict[str, str]:
    """Assert every gold substring occurs in its named doc (after the same
    s2twp normalization ingest applies), so a fixture typo fails loudly
    rather than silently scoring a miss. Returns {doc_stem: normalized_text}."""
    norm_docs = {p.stem: convert(p.read_text(encoding="utf-8"))
                 for p in sorted(DOCS_DIR.glob("*.txt"))}
    problems = []
    for item in golden:
        for exp in item["expect"]:
            doc, text = exp["doc"], exp["text"]
            if doc not in norm_docs:
                problems.append(f"{item['id']}: unknown doc '{doc}'")
            elif text not in norm_docs[doc]:
                problems.append(f"{item['id']}: gold text not in {doc}: {text!r}")
            else:
                hits = [d for d, body in norm_docs.items() if text in body]
                if len(hits) > 1:
                    problems.append(
                        f"{item['id']}: gold text ambiguous (in {hits}): {text!r}")
    if problems:
        raise SystemExit("Fixture validation failed:\n  " + "\n  ".join(problems))
    return norm_docs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=5, help="top-k for recall@k")
    parser.add_argument("--max-dist", type=float, default=None,
                        help="override MAX_VECTOR_DISTANCE for a tuning sweep")
    args = parser.parse_args()
    k = args.k

    tmp = _bootstrap_env()

    # App imports happen only after the env is set (see _bootstrap_env).
    from app import db
    from app.config import load_settings
    from app.ingest.pipeline import ingest_text
    from app.rag import chat as chat_mod
    from app.rag.chat import RRF_K, _fts_query, retrieve
    from app.rag.embedder import Embedder, vector_to_blob
    from app.textnorm import s2twp

    # Threshold sweep: patch the module global retrieve() reads, and mirror it
    # in this script's own vector_topk so attribution uses the same cutoff.
    max_dist = args.max_dist if args.max_dist is not None \
        else chat_mod.MAX_VECTOR_DISTANCE
    chat_mod.MAX_VECTOR_DISTANCE = max_dist

    golden = load_golden()
    validate_fixture(golden, s2twp.convert)

    settings = load_settings(require_keys=False)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    embedder = Embedder(settings.embedding_model)

    # --- ingest the corpus through the real pipeline (regresses ingest too) ---
    doc_of_source: dict[int, str] = {}
    docs = sorted(DOCS_DIR.glob("*.txt"))
    for path in docs:
        outcome = ingest_text(path.read_text(encoding="utf-8"), settings, conn,
                              embedder=embedder, title=path.stem)
        doc_of_source[outcome.source_id] = path.stem
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    # --- per-query evaluation ---
    def vector_topk(question: str) -> list[int]:
        qvec = vector_to_blob(embedder.embed_query(question))
        return [r["chunk_id"] for r in conn.execute(
            "SELECT chunk_id FROM chunk_vectors WHERE embedding MATCH ?"
            " AND k = ? AND distance <= ? ORDER BY distance",
            (qvec, k, max_dist))]

    def fts_topk(question: str) -> list[int]:
        fts = _fts_query(question)
        if not fts:
            return []
        try:
            return [r["rowid"] for r in conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?"
                " ORDER BY rank LIMIT ?", (fts, k))]
        except Exception:  # noqa: BLE001 — mirror retrieve()'s FTS-syntax guard
            return []

    per_cat_hits: dict[str, list[float]] = defaultdict(list)
    per_cat_rr: dict[str, list[float]] = defaultdict(list)
    attribution = {"both": 0, "vector_only": 0, "fts_only": 0, "neither": 0}
    coverages: list[float] = []
    rows_out = []
    negatives = 0          # queries that should return nothing (expect == [])
    false_hits = 0         # ...but returned something anyway

    for item in golden:
        chunks = retrieve(conn, embedder, item["query"], top_k=k)

        # Negative query (off-topic): a good system returns nothing. Measures
        # the precision cost of loosening the distance threshold, which a
        # recall-only set would hide.
        if not item["expect"]:
            negatives += 1
            returned = len(chunks)
            if returned:
                false_hits += 1
            rows_out.append({"id": item["id"], "category": item["category"],
                             "hit": None, "rank": None,
                             "attribution": f"returned {returned}",
                             "query": item["query"]})
            continue

        # First rank at which any expected passage appears (1-based); and how
        # many distinct expected passages are covered within top-k.
        first_rank = None
        matched_ids = []
        covered = 0
        for exp in item["expect"]:
            exp_rank = None
            for rank, c in enumerate(chunks, start=1):
                if exp["text"] in c.text:
                    exp_rank = rank
                    matched_ids.append(c.chunk_id)
                    break
            if exp_rank is not None:
                covered += 1
                if first_rank is None or exp_rank < first_rank:
                    first_rank = exp_rank

        hit = 1.0 if first_rank is not None else 0.0
        rr = (1.0 / first_rank) if first_rank else 0.0
        per_cat_hits[item["category"]].append(hit)
        per_cat_rr[item["category"]].append(rr)
        coverages.append(covered / len(item["expect"]))

        # Attribution: for the first matched gold chunk, which route(s) had it
        # in their own top-k? This is the mixed-retrieval "who saved whom".
        attr = "—"
        if matched_ids:
            cid = matched_ids[0]
            in_vec = cid in vector_topk(item["query"])
            in_fts = cid in fts_topk(item["query"])
            attr = ("both" if in_vec and in_fts else
                    "vector_only" if in_vec else
                    "fts_only" if in_fts else "neither")
            attribution[attr] += 1

        rows_out.append({"id": item["id"], "category": item["category"],
                         "hit": hit, "rank": first_rank, "attribution": attr,
                         "query": item["query"]})

    all_hits = [h for hs in per_cat_hits.values() for h in hs]
    all_rr = [r for rs in per_cat_rr.values() for r in rs]
    recall = sum(all_hits) / len(all_hits)
    mrr = sum(all_rr) / len(all_rr)

    # --- report ---
    print(f"\nvidrag retrieval eval — {len(golden)} queries, {len(docs)} docs, "
          f"{n_chunks} chunks, k={k}")
    print(f"model={embedder.model_name}  RRF_K={RRF_K}  "
          f"max_dist={max_dist}")
    print("-" * 60)
    print(f"{'category':<14}{'n':>4}{'recall@'+str(k):>10}{'MRR':>8}")
    for cat in sorted(per_cat_hits):
        hs, rs = per_cat_hits[cat], per_cat_rr[cat]
        print(f"{cat:<14}{len(hs):>4}{sum(hs)/len(hs):>10.2f}"
              f"{sum(rs)/len(rs):>8.2f}")
    print("-" * 60)
    print(f"{'OVERALL':<14}{len(all_hits):>4}{recall:>10.2f}{mrr:>8.2f}")
    print(f"cross-source coverage (mean expected passages found): "
          f"{sum(coverages)/len(coverages):.2f}")
    print("\nroute attribution (who surfaced the gold chunk):")
    for kind in ("both", "vector_only", "fts_only", "neither"):
        print(f"  {kind:<12}{attribution[kind]:>3}")
    if negatives:
        print(f"\nnegatives (should return nothing): {negatives}  "
              f"false-hits: {false_hits}  "
              f"(precision-safe if 0)")
    misses = [r for r in rows_out if r["hit"] == 0.0]
    if misses:
        print(f"\nmisses ({len(misses)}):")
        for r in misses:
            print(f"  [{r['category']}] {r['id']}: {r['query']}")

    # --- append to history (offline, deterministic timestamp from wall clock) ---
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "k": k, "model": embedder.model_name, "rrf_k": RRF_K,
        "max_vector_distance": max_dist,
        "n_docs": len(docs), "n_chunks": n_chunks, "n_queries": len(golden),
        f"recall@{k}": round(recall, 4), "mrr": round(mrr, 4),
        "coverage": round(sum(coverages) / len(coverages), 4),
        "negatives": negatives, "false_hits": false_hits,
        "attribution": attribution,
        "recall_by_category": {c: round(sum(h) / len(h), 4)
                               for c, h in per_cat_hits.items()},
    }
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\nappended to {HISTORY.relative_to(CORPUS_DIR.parent.parent)}")

    # Machine-readable summary line (picked up by run_all_tests.py). This is a
    # measurement, not a gate — it always exits 0; regressions are judged by
    # comparing against eval_history.jsonl, not by pass/fail here.
    print(f"PASS retrieval eval recall@{k}={recall:.2f} mrr={mrr:.2f} "
          f"(vec_only={attribution['vector_only']} "
          f"fts_only={attribution['fts_only']})")

    conn.close()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)   # throwaway eval DB
    return 0


if __name__ == "__main__":
    sys.exit(main())
