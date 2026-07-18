# Offline acceptance checks for Phase1-5 (transcribe.py) — everything
# except the Groq API call itself (needs GROQ_API_KEY, verified separately).
# Run: .venv/Scripts/python scripts/verify_transcribe_offline.py
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import transcribe as t

logging.basicConfig(level=logging.INFO)
failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # --- vocabulary prompt: small file intact, big file truncated with warning ---
    small = tmp / "vocab_small.txt"
    small.write_text("台積電\n聯發科\n# comment\nNVIDIA\n", encoding="utf-8")
    p = t.build_vocab_prompt(small)
    check("vocab small intact", p == "台積電, 聯發科, NVIDIA", repr(p))

    big = tmp / "vocab_big.txt"
    big.write_text("\n".join(f"超長專有名詞編號第{i}條" for i in range(100)),
                   encoding="utf-8")
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("app.ingest.transcribe").addHandler(handler)
    p2 = t.build_vocab_prompt(big)
    logging.getLogger("app.ingest.transcribe").removeHandler(handler)
    check("vocab truncated under budget",
          p2 is not None and t.estimate_tokens(p2) <= t.VOCAB_TOKEN_LIMIT,
          f"{t.estimate_tokens(p2)} tokens")
    check("vocab truncation logged (not silent)",
          "token prompt budget" in stream.getvalue(), stream.getvalue().strip()[:80])

    # --- hallucination filter ---
    raw = (
        [{"text": "正常句子A", "start": 0, "end": 1, "no_speech_prob": 0.1}]
        + [{"text": "謝謝觀看", "start": i, "end": i + 1, "no_speech_prob": 0.1}
           for i in range(1, 9)]  # classic whisper loop x8
        # true silence hallucination: no-speech AND low decode confidence
        + [{"text": "背景音樂", "start": 9, "end": 10,
            "no_speech_prob": 0.95, "avg_logprob": -1.5}]
        # speech over music: high no_speech_prob but confident decode -> KEEP
        + [{"text": "疊音樂的真語音", "start": 10, "end": 11,
            "no_speech_prob": 0.95, "avg_logprob": -0.12}]
        + [{"text": "  ", "start": 11, "end": 12, "no_speech_prob": 0.1}]
        + [{"text": "正常句子B", "start": 12, "end": 13, "no_speech_prob": 0.2}]
    )
    kept = t.filter_segments(raw)
    texts = [s["text"].strip() for s in kept]
    check("filter drops no-speech hallucination", "背景音樂" not in texts)
    check("filter keeps speech over music", "疊音樂的真語音" in texts)
    check("filter collapses repeats",
          texts.count("謝謝觀看") == t.REPEAT_LIMIT, str(texts))
    check("filter keeps normal", "正常句子A" in texts and "正常句子B" in texts)

    # --- phantom end-card / subscribe hallucinations: Whisper decodes these
    #     with HIGH confidence on music, so the general no-speech+logprob filter
    #     misses them. The phantom set catches them, which un-blocks vision for
    #     music-only clips (the source-70 bug, 2026-07-18) ---
    phantom_raw = [
        {"text": "Thank you for watching!", "start": 0, "end": 2,
         "no_speech_prob": 0.92, "avg_logprob": -0.08},   # confident decode
        {"text": "谢谢观看", "start": 2, "end": 4,
         "no_speech_prob": 0.88, "avg_logprob": -0.05},
    ]
    check("filter drops confident phantom end-cards",
          t.filter_segments(phantom_raw) == [], str(t.filter_segments(phantom_raw)))
    real = [{"text": "Thanks for watching", "start": 0, "end": 2,
             "no_speech_prob": 0.05, "avg_logprob": -0.1}]
    check("filter keeps low-no-speech 'thanks for watching'",
          len(t.filter_segments(real)) == 1, str(t.filter_segments(real)))
    from app.rag.vision import should_run_vision
    check("phantom-only clip becomes hollow -> vision triggers",
          should_run_vision(False, [], 9.0) is True)

    # --- ffmpeg preprocess / probe / split on synthesized audio ---
    src = tmp / "tone.wav"
    t._run(
        [t._ffmpeg_bin("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=30", str(src)],
        stage="synth",
    )
    out = t.preprocess_audio(src, tmp)
    check("preprocess produces mp3", out.exists() and out.suffix == ".mp3",
          f"{out.stat().st_size} bytes")
    dur = t.probe_duration(out)
    check("probe duration ~30s", 29 <= dur <= 31, f"{dur:.2f}s")

    # force split by shrinking thresholds
    orig_max, orig_target = t.MAX_UPLOAD_BYTES, t.TARGET_PART_BYTES
    t.MAX_UPLOAD_BYTES, t.TARGET_PART_BYTES = 40_000, 30_000
    try:
        parts = t.split_audio(out, tmp)
        check("split into parts", len(parts) >= 2, f"{len(parts)} parts")
        check("split offsets monotonic",
              all(parts[i][1] < parts[i + 1][1] for i in range(len(parts) - 1)),
              str([f"{off:.1f}" for _, off in parts]))
        total = sum(t.probe_duration(p) for p, _ in parts)
        check("split covers full duration", abs(total - dur) < 2.0,
              f"{total:.2f}s vs {dur:.2f}s")
    finally:
        t.MAX_UPLOAD_BYTES, t.TARGET_PART_BYTES = orig_max, orig_target

    # --- opencc conversion inside segment build (via shared singleton) ---
    from app.textnorm import s2twp

    check("s2twp", s2twp.convert("这是简体字的内存") == "這是簡體字的記憶體")

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
