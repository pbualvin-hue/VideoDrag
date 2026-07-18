# Acceptance check for Phase1-4 (youtube adapter). Hits the real network.
# Run: .venv/Scripts/python scripts/verify_youtube.py [captioned_url] [uncaptioned_url]
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import youtube
from app.ingest.base import VideoTooLongError
from app.ingest.normalize import resolve

# Defaults: TED talk (manual multi-language subtitles) and
# "Me at the zoo" (no manually created subtitles, 19s -> tiny audio).
captioned = sys.argv[1] if len(sys.argv) > 1 else "https://youtu.be/iG9CE55wbtY"
uncaptioned = sys.argv[2] if len(sys.argv) > 2 else "https://youtu.be/jNQXAC9IVRw"

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        failures.append(name)


with tempfile.TemporaryDirectory() as td:
    workdir = Path(td)

    # --- captioned video: transcript path, no audio download ---
    norm = resolve(captioned)
    result = youtube.fetch(norm, workdir, max_duration_secs=10800)
    check("captioned: has transcript", result.has_transcript,
          f"{len(result.transcript)} segments")
    check("captioned: no audio file", result.audio_path is None
          and not list(workdir.iterdir()))
    md = result.metadata
    check("captioned: metadata complete",
          bool(md.title and md.published_at and md.duration_secs),
          f"title={md.title[:40]!r} published={md.published_at} dur={md.duration_secs}s")
    if result.transcript:
        seg = result.transcript[0]
        check("captioned: segment timing sane",
              seg.end_sec > seg.start_sec >= 0,
              f"[{seg.start_sec:.1f}-{seg.end_sec:.1f}] {seg.text[:50]!r}")

    # --- too-long rejection uses metadata, no download ---
    try:
        youtube.fetch(norm, workdir, max_duration_secs=10)
        check("too-long rejected", False, "accepted!")
    except VideoTooLongError as e:
        check("too-long rejected", True, e.stage)

    # --- uncaptioned video: audio fallback ---
    norm2 = resolve(uncaptioned)
    result2 = youtube.fetch(norm2, workdir, max_duration_secs=10800)
    check("uncaptioned: audio downloaded",
          result2.audio_path is not None and result2.audio_path.exists(),
          str(result2.audio_path))
    check("uncaptioned: no transcript", not result2.has_transcript)
    if result2.audio_path:
        size = result2.audio_path.stat().st_size
        check("uncaptioned: audio non-empty", size > 1000, f"{size} bytes")
    check("uncaptioned: metadata complete",
          bool(result2.metadata.title and result2.metadata.published_at),
          f"published={result2.metadata.published_at}")

print()
if failures:
    print(f"FAILED: {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
