"""Generate the Chrome extension's PNG icons: an amber bookmark ("save").

A free-floating amber (#D9B26A) bookmark with a down-arrow, on a transparent
background — chosen over a dark tile because the graphite tile blended into the
user's dark browser toolbar (2026-07-18). Amber is a mid-tone that reads on
both light and dark toolbars; a thin graphite outline keeps the edge crisp on
light backgrounds. Renders at 4x then downsamples (LANCZOS) for clean edges at
16/32/48/128 px.

Run:  .venv/Scripts/python scripts/gen_extension_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "extension"
SIZES = (16, 32, 48, 128)
SUPER = 512  # supersample canvas, then downscale

AMBER = (217, 178, 106, 255)    # #D9B26A accent (Editorial Terminal theme)
OUTLINE = (23, 23, 26, 235)     # #17171A warm graphite, for edge definition
INK = (23, 23, 26, 255)         # down-arrow glyph inside the bookmark


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (SUPER, SUPER), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = SUPER
    x0, x1 = 0.30 * s, 0.70 * s
    ytop, ybot, ynotch, xmid = 0.15 * s, 0.85 * s, 0.66 * s, 0.50 * s
    # Classic bookmark silhouette: square top, V-notch bottom.
    pts = [(x0, ytop), (x1, ytop), (x1, ybot), (xmid, ynotch), (x0, ybot)]
    d.polygon(pts, fill=AMBER)
    d.line([*pts, pts[0]], fill=OUTLINE, width=int(0.045 * s), joint="curve")
    # Down-arrow = "save into" (kept simple so it survives 16px).
    cx = xmid
    d.line([(cx, 0.28 * s), (cx, 0.50 * s)], fill=INK, width=int(0.05 * s))
    d.line([(cx - 0.07 * s, 0.43 * s), (cx, 0.51 * s), (cx + 0.07 * s, 0.43 * s)],
           fill=INK, width=int(0.05 * s), joint="curve")
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    for size in SIZES:
        path = OUT / f"icon{size}.png"
        render(size).save(path, "PNG")
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
