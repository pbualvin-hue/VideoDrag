"""Audio -> transcript via Groq whisper-large-v3 (full model, not turbo).

Pipeline (CLAUDE.md rule 3):
  downsample 16kHz mono low-bitrate -> split if >25MB upload limit ->
  Groq verbose_json (vocabulary initial_prompt, <=224 tokens) ->
  hallucination filter (no_speech_prob + repeated-sentence) ->
  OpenCC s2twp -> segments with a merged timeline.

ffmpeg/ffprobe are resolved from FFMPEG_PATH's directory when set,
otherwise from PATH (the Docker image guarantees them).
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from groq import Groq

from ..config import GROQ_WHISPER_MODEL
from ..textnorm import s2twp
from .base import TranscriptSegment

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024      # Groq hard limit
TARGET_PART_BYTES = 20 * 1024 * 1024     # split target, safety margin
VOCAB_TOKEN_LIMIT = 224                  # whisper initial_prompt budget
# Whisper reference heuristic: a segment is "silence hallucination" only when
# BOTH the no-speech detector fires AND decode confidence is poor. Speech over
# background music routinely scores no_speech_prob 0.8+ with high confidence
# (measured live on Groq, 2026-07): filtering on no_speech_prob alone throws
# away real content.
NO_SPEECH_THRESHOLD = 0.6
LOGPROB_THRESHOLD = -1.0
REPEAT_LIMIT = 2                         # keep at most N consecutive identical sentences


class TranscribeError(Exception):
    def __init__(self, stage: str, original: str):
        self.stage = stage
        self.original = original
        super().__init__(f"transcribe {stage} failed: {original}")


@dataclass(frozen=True)
class TranscriptionResult:
    segments: list[TranscriptSegment]
    language: str | None
    audio_duration_secs: float


def _ffmpeg_bin(name: str) -> str:
    override = os.environ.get("FFMPEG_PATH")
    if override:
        return str(Path(override).parent / f"{name}{Path(override).suffix}")
    return name


def _run(cmd: list[str], stage: str) -> str:
    # encoding pinned: Windows would otherwise decode ffmpeg stderr with the
    # legacy codepage and mask real errors behind UnicodeDecodeError.
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise TranscribeError(stage, proc.stderr[-2000:])
    return proc.stdout


def estimate_tokens(text: str) -> int:
    """Conservative whisper-token estimate: CJK chars count as ~1.5 tokens,
    everything else ~1 token per 3 characters."""
    cjk = len(re.findall(r"[一-鿿㐀-䶿]", text))
    other = len(text) - cjk
    return math.ceil(cjk * 1.5 + other / 3)


def build_vocab_prompt(vocab_path: Path) -> str | None:
    """Join vocabulary lines into an initial_prompt, truncated to the token
    budget by taking a prefix of lines. Truncation is logged, never silent
    (CLAUDE.md rule 3)."""
    if not vocab_path.exists():
        return None
    lines = [
        line.strip()
        for line in vocab_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not lines:
        return None
    kept: list[str] = []
    for line in lines:
        candidate = ", ".join(kept + [line])
        if estimate_tokens(candidate) > VOCAB_TOKEN_LIMIT:
            logger.warning(
                "vocabulary.txt exceeds %d-token prompt budget: kept %d/%d entries",
                VOCAB_TOKEN_LIMIT, len(kept), len(lines),
            )
            break
        kept.append(line)
    return ", ".join(kept) if kept else None


def preprocess_audio(src: Path, workdir: Path) -> Path:
    """Downsample to 16kHz mono 32kbps mp3 — always, so upload size is
    predictable regardless of the source container."""
    out = workdir / f"{src.stem}_16k.mp3"
    _run(
        [_ffmpeg_bin("ffmpeg"), "-y", "-i", str(src),
         "-ar", "16000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "32k",
         "-vn", str(out)],
        stage="preprocess",
    )
    return out


def has_audio_stream(path: Path) -> bool:
    """True if the file carries an audio stream. A silent video (no audio,
    e.g. an IG slideshow reel) must route to vision (rule 3) rather than
    hard-fail ffmpeg's -vn audio preprocess."""
    out = _run(
        [_ffmpeg_bin("ffprobe"), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        stage="probe_audio",
    )
    return "audio" in out


def probe_duration(path: Path) -> float:
    out = _run(
        [_ffmpeg_bin("ffprobe"), "-v", "error", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(path)],
        stage="probe",
    )
    try:
        return float(out.strip())
    except ValueError as exc:
        raise TranscribeError("probe", f"unparseable duration: {out!r}") from exc


def split_audio(path: Path, workdir: Path) -> list[tuple[Path, float]]:
    """Split into <=TARGET_PART_BYTES parts; returns (part_path, offset_secs).

    Known limitation: `-c copy` cuts on mp3 frame boundaries, so real cut
    points can drift slightly from the theoretical offsets used for the
    merged timeline (sub-second per part at 32kbps CBR — acceptable for
    chunk-level timestamps)."""
    size = path.stat().st_size
    if size <= MAX_UPLOAD_BYTES:
        return [(path, 0.0)]
    duration = probe_duration(path)
    n_parts = math.ceil(size / TARGET_PART_BYTES)
    part_secs = duration / n_parts
    parts: list[tuple[Path, float]] = []
    for i in range(n_parts):
        offset = i * part_secs
        out = workdir / f"{path.stem}_part{i}.mp3"
        _run(
            [_ffmpeg_bin("ffmpeg"), "-y", "-ss", f"{offset:.3f}",
             "-t", f"{part_secs:.3f}", "-i", str(path), "-c", "copy", str(out)],
            stage="split",
        )
        parts.append((out, offset))
    logger.info("audio split into %d parts of ~%.0fs", n_parts, part_secs)
    return parts


def _normalize_sentence(text: str) -> str:
    return re.sub(r"[\s。,,.!?!?~…·、]+", "", text).lower()


# Whisper emits a small set of stock phrases on silence/music — video end-cards
# and subscribe boilerplate — that decode with HIGH confidence, so the general
# no_speech+logprob filter (which needs BOTH signals) misses them. They defeat
# hollow-transcript detection (a music-only clip looks "spoken") and pollute the
# RAG. We drop them when the no-speech detector fires; genuine "thanks for
# watching" speech scores LOW no_speech_prob and is kept. Both simplified and
# traditional variants are listed (filtering runs before OpenCC conversion).
# Maintenance rule: only add a phrase that is unambiguous end-card/subscribe
# boilerplate — NOT bare closings a real speaker says ("谢谢大家", "大家好"),
# which the no_speech gate alone can't distinguish from a genuine sign-off.
_PHANTOM_PHRASES = frozenset(_normalize_sentence(p) for p in (
    "Thank you for watching", "Thanks for watching",
    "Thank you for watching this video", "Please subscribe",
    "Like and subscribe", "Thanks for watching!",
    "谢谢观看", "謝謝觀看", "谢谢大家观看", "謝謝大家觀看",
    "请订阅", "請訂閱", "请点赞订阅", "請點贊訂閱",
    "请不吝点赞 订阅 转发 打赏", "請不吝點贊 訂閱 轉發 打賞",
    "明镜与点点栏目", "字幕志愿者",
    "ご視聴ありがとうございました", "시청해주셔서 감사합니다",
))
PHANTOM_NO_SPEECH_THRESHOLD = 0.5   # phantom phrase + this no-speech signal -> drop


def filter_segments(raw_segments: list[dict]) -> list[dict]:
    """Drop hallucinations: known phantom end-card/subscribe phrases on
    non-speech audio, high no_speech_prob silence, and runaway repeats."""
    kept: list[dict] = []
    prev_norm = None
    repeat_count = 0
    dropped_phantom = dropped_ns = dropped_rep = 0
    for seg in raw_segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        norm = _normalize_sentence(text)
        ns_prob = seg.get("no_speech_prob", 0.0)
        # 1. Known phantom phrase co-occurring with any no-speech signal.
        if norm in _PHANTOM_PHRASES and ns_prob > PHANTOM_NO_SPEECH_THRESHOLD:
            dropped_phantom += 1
            continue
        # 2. Generic silence hallucination: no-speech AND poor decode confidence.
        if ns_prob > NO_SPEECH_THRESHOLD and seg.get("avg_logprob", 0.0) < LOGPROB_THRESHOLD:
            dropped_ns += 1
            continue
        # 3. Runaway repeated sentence.
        if norm and norm == prev_norm:
            repeat_count += 1
            if repeat_count >= REPEAT_LIMIT:
                dropped_rep += 1
                continue
        else:
            repeat_count = 0
        prev_norm = norm
        kept.append(seg)
    if dropped_phantom or dropped_ns or dropped_rep:
        logger.info(
            "hallucination filter dropped %d phantom + %d no-speech + %d repeated",
            dropped_phantom, dropped_ns, dropped_rep,
        )
    return kept


def _segments_of(response: object) -> list[dict]:
    segments = getattr(response, "segments", None) or []
    return [s if isinstance(s, dict) else dict(s) for s in segments]


def transcribe(
    audio_path: Path,
    workdir: Path,
    api_key: str,
    vocab_path: Path | None = None,
) -> TranscriptionResult:
    """Full transcription flow. Caller owns temp-file cleanup of `workdir`."""
    client = Groq(api_key=api_key)
    prompt = build_vocab_prompt(vocab_path) if vocab_path else None

    processed = preprocess_audio(audio_path, workdir)
    duration = probe_duration(processed)
    parts = split_audio(processed, workdir)

    all_raw: list[dict] = []
    language: str | None = None
    for part_path, offset in parts:
        try:
            with open(part_path, "rb") as fh:
                response = client.audio.transcriptions.create(
                    file=(part_path.name, fh.read()),
                    model=GROQ_WHISPER_MODEL,
                    response_format="verbose_json",
                    temperature=0.0,
                    **({"prompt": prompt} if prompt else {}),
                )
        except Exception as exc:
            raise TranscribeError("groq_api", str(exc)) from exc
        language = language or getattr(response, "language", None)
        for seg in _segments_of(response):
            seg["start"] = float(seg.get("start", 0.0)) + offset
            seg["end"] = float(seg.get("end", 0.0)) + offset
            all_raw.append(seg)

    kept = filter_segments(all_raw)
    segments = [
        TranscriptSegment(
            text=s2twp.convert((seg.get("text") or "").strip()),
            start_sec=seg["start"],
            end_sec=seg["end"],
        )
        for seg in kept
    ]
    return TranscriptionResult(
        segments=segments, language=language, audio_duration_secs=duration
    )
