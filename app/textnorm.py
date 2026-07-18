"""Shared text normalization singletons.

OpenCC loads dictionaries on construction; keep one instance per process.
s2twp = Simplified -> Taiwan Traditional with Taiwanese phrasing
(CLAUDE.md rule 3: applies to transcripts AND metadata like titles).
"""

import re

from opencc import OpenCC

s2twp = OpenCC("s2twp")

# Common interrogative fragments, stripped before building an FTS query. Without
# this, a whole Chinese question ("乳酸閾值是什麼") becomes ONE long term under
# `[一-鿿]{3,}` — a phrase the trigram FTS index can never match. Stripping the
# fragment leaves the content term ("乳酸閾值") to actually search.
#
# LIMITS (deliberately conservative — see code review 2026-07-18): only genuine
# interrogatives that are ~never standalone content nouns are listed, so we don't
# silently blank out real queries like "概念股" or "代表人". This mainly helps
# when the interrogative is *separable*; a leading form ("什麼是X") or a 的-
# possessive ("X的意思") can still leave a term with an extra glued character
# that misses. It never regresses the old behaviour (which missed the whole
# question anyway). Longer fragments precede their own substrings so replacement
# is greedy-correct. Validated by scripts/eval_retrieval.py.
_FTS_STOP = ("為什麼", "是什麼", "怎麼樣", "怎麼辦", "怎麼", "什麼",
             "哪一個", "哪個", "哪些", "如何")


def build_fts_query(question: str) -> str | None:
    """Build an OR-of-quoted-terms FTS5 query from a natural-language question.

    The trigram tokenizer needs >=3 chars per quoted Chinese term; latin/ticker
    tokens (>=2 chars) are kept as-is. Interrogative fragments are stripped
    first (see _FTS_STOP). Returns None when nothing searchable remains, so
    callers fall back to the vector arm (or return no FTS hits) instead of
    issuing a malformed MATCH.
    """
    for frag in _FTS_STOP:
        question = question.replace(frag, " ")
    terms = re.findall(r"[A-Za-z0-9]{2,}|[一-鿿]{3,}", question)
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))
