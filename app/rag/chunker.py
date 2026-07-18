"""Transcript -> chunks: 300–500 tokens each, 15% overlap, timestamps kept.

The stored chunk text is pure content; the video-title prefix is added only
at embedding time (CLAUDE.md 逐字稿切塊規格).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..ingest.base import TranscriptSegment

MIN_TOKENS = 300
MAX_TOKENS = 500
OVERLAP_RATIO = 0.15

_CJK_RE = re.compile(r"[一-鿿㐀-䶿぀-ゟ゠-ヿ가-힣]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。!?!?;;\.])\s*")


@dataclass(frozen=True)
class Chunk:
    text: str
    start_sec: float
    end_sec: float


def count_tokens(text: str) -> int:
    """Realistic estimate for chunk sizing (unlike the deliberately
    conservative whisper-prompt estimator): CJK char ~1 token,
    other text ~1 token per 4 chars."""
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + max(0, other) // 4


def _split_oversize(seg: TranscriptSegment) -> list[TranscriptSegment]:
    """Sentence-split a single segment that alone exceeds MAX_TOKENS,
    interpolating timestamps by character position."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(seg.text) if s.strip()]
    if len(sentences) <= 1:
        # No sentence breaks (e.g. a pasted blob): fall back to fixed
        # character windows so a single huge segment can't sail past the
        # embedder's window with only its head indexed (audit 2026-07-14).
        # CJK is ~1 token/char, so MAX_TOKENS-char windows never exceed
        # MAX_TOKENS; undersized pieces get re-packed by the greedy loop.
        sentences = [seg.text[i:i + MAX_TOKENS]
                     for i in range(0, len(seg.text), MAX_TOKENS)]
        if len(sentences) <= 1:
            return [seg]
    total_chars = sum(len(s) for s in sentences)
    span = seg.end_sec - seg.start_sec
    out: list[TranscriptSegment] = []
    pos = 0
    for s in sentences:
        start = seg.start_sec + span * (pos / total_chars)
        pos += len(s)
        end = seg.start_sec + span * (pos / total_chars)
        out.append(TranscriptSegment(text=s.strip(), start_sec=start, end_sec=end))
    return out


def chunk_transcript(segments: list[TranscriptSegment]) -> list[Chunk]:
    """Greedy packing with a 15% token-overlap tail carried between chunks."""
    prepared: list[TranscriptSegment] = []
    for seg in segments:
        if count_tokens(seg.text) > MAX_TOKENS:
            prepared.extend(_split_oversize(seg))
        elif seg.text.strip():
            prepared.append(seg)

    chunks: list[Chunk] = []
    current: list[TranscriptSegment] = []
    has_new_content = False  # does `current` hold anything beyond the overlap tail?

    def joined_tokens(segs: list[TranscriptSegment]) -> int:
        # Measure the actual stored text, separators included — per-segment
        # sums undercount because token flooring drops joint punctuation.
        return count_tokens(" ".join(s.text for s in segs))

    def close_current() -> None:
        nonlocal current, has_new_content
        if not current:
            return
        chunks.append(
            Chunk(
                text=" ".join(s.text for s in current),
                start_sec=current[0].start_sec,
                end_sec=current[-1].end_sec,
            )
        )
        # Seed the next chunk with a ~15% token tail for context continuity.
        overlap_budget = int(joined_tokens(current) * OVERLAP_RATIO)
        tail: list[TranscriptSegment] = []
        for seg in reversed(current):
            candidate = [seg] + tail
            if joined_tokens(candidate) > overlap_budget:
                break
            tail = candidate
        # Best-effort floor: when even the last segment exceeds the budget,
        # still carry it (unless that alone would crowd out the next chunk).
        if not tail and count_tokens(current[-1].text) < MIN_TOKENS:
            tail = [current[-1]]
        current = tail
        has_new_content = False

    for seg in prepared:
        if current and joined_tokens(current + [seg]) > MAX_TOKENS:
            close_current()
        current.append(seg)
        has_new_content = True

    # Final partial chunk: emit only if it holds content beyond the seeded
    # overlap tail (end_sec comparisons are unreliable as a proxy).
    if current and has_new_content:
        chunks.append(
            Chunk(
                text=" ".join(s.text for s in current),
                start_sec=current[0].start_sec,
                end_sec=current[-1].end_sec,
            )
        )
    return chunks
