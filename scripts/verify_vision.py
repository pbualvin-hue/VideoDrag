# Acceptance for Phase4-3 (vision path). Synthesizes an info-dense slide
# video, tests the judgment chain + frame extraction locally, then reads
# frames with Claude (needs ANTHROPIC_API_KEY).
# Run: .venv/Scripts/python -X utf8 scripts/verify_vision.py
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings
from app.ingest.base import TranscriptSegment
from app.ingest.transcribe import _ffmpeg_bin, _run
from app.rag import vision

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


# --- judgment chain ---
segs_full = [TranscriptSegment("x", i * 1.0, i * 1.0 + 0.9) for i in range(60)]
check("captions -> skip vision",
      vision.should_run_vision(True, segs_full, 60) is False)
check("full speech -> skip vision",
      vision.should_run_vision(False, segs_full, 60) is False,
      f"coverage={vision.speech_coverage(segs_full, 60):.2f}")
segs_sparse = [TranscriptSegment("x", 0, 5)]  # 5s speech in a 60s clip
check("hollow transcript -> run vision",
      vision.should_run_vision(False, segs_sparse, 60) is True,
      f"coverage={vision.speech_coverage(segs_sparse, 60):.2f}")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    src = tmp / "slides.mp4"
    # Copy a CJK font locally so drawtext works without fontconfig (Windows).
    import shutil as _sh
    font_src = next((p for p in [
        Path("C:/Windows/Fonts/msjh.ttc"), Path("C:/Windows/Fonts/mingliu.ttc"),
        Path("C:/Windows/Fonts/arial.ttf")] if p.exists()), None)
    if font_src is None:
        print("SKIP synth (no usable font found)"); sys.exit(0)
    _sh.copy(font_src, tmp / "font.ttf")
    # Three distinct text "slides" (5s each) -> scene changes at 5s, 10s.
    vf = ("drawtext=fontfile=font.ttf:text='台積電 Q4 毛利率 53%%':fontsize=40:"
          "x=30:y=100:fontcolor=white:enable='between(t,0,5)',"
          "drawtext=fontfile=font.ttf:text='營收年增 20%% 三大法人買超':fontsize=36:"
          "x=30:y=100:fontcolor=yellow:enable='between(t,5,10)',"
          "drawtext=fontfile=font.ttf:text='目標價 600 元 建議留意':fontsize=36:"
          "x=30:y=100:fontcolor=cyan:enable='between(t,10,15)'")
    proc = subprocess.run(
        [_ffmpeg_bin("ffmpeg"), "-y", "-f", "lavfi",
         "-i", "color=c=black:s=640x360:d=15",
         "-vf", vf, "-r", "10", str(src)],
        cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print("SKIP synth (ffmpeg drawtext failed):", proc.stderr[-300:]); sys.exit(0)

    frames = vision.extract_keyframes(src, tmp / "frames")
    check("keyframes extracted", len(frames) >= 2, f"{len(frames)} frames")
    check("frames timestamped",
          all(ts >= 0 for _, ts in frames))

    cover = vision.save_cover_thumbnail(frames, tmp / "cover.jpg")
    check("cover thumbnail saved", cover is not None and cover.exists())

    settings = load_settings(require_keys=False)
    if not settings.anthropic_api_key:
        print("SKIP vision read (no ANTHROPIC_API_KEY)")
    else:
        usage_log = []
        chunks = vision.analyze_frames(
            frames, settings, lambda m, u: usage_log.append(m))
        joined = " ".join(c.text for c in chunks)
        check("vision read produced chunks", len(chunks) >= 1,
              f"{len(chunks)} chunks")
        check("vision read on-screen text",
              any(k in joined for k in ("台積", "53", "600", "毛利", "營收")),
              joined[:120])
        check("pre-judge + read billed", len(usage_log) == 2, str(usage_log))

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
