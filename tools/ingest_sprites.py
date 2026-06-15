"""Normalize freshly generated sprite art into the project's clean sprite spec.

Reads raw PNGs from assets/_incoming/, then for every recognised frame:
  - optionally keys out a flat background colour
  - binarizes the alpha channel (kills the soft anti-aliased halo -> crisp edges)
  - trims to content, then scales ALL frames by ONE factor (derived from the
    idle master) so the character keeps a consistent size across frames
  - bottom-anchors + horizontally centers on a TARGET x TARGET transparent canvas
    with a fixed top headroom (fixes the "looks like the head is clipped" feel)

Output goes to assets/moonshell_96/ for review plus a contact sheet at
docs/_ingest_preview.png. It NEVER touches the live assets/moonshell/ folder;
swapping in the new art (and bumping the code constants from 48 to TARGET) is a
separate, deliberate step.

Usage:
    python tools/ingest_sprites.py
    python tools/ingest_sprites.py --target 96 --top 10 --bottom 4 --alpha-thresh 128
    python tools/ingest_sprites.py --bg "#FF00FF"      # chroma-key a magenta background
    python tools/ingest_sprites.py --mirror-walk        # build walk_left_* by flipping walk_right_*
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "_incoming"
PREVIEW = ROOT / "docs" / "_ingest_preview.png"

FRAMES = [
    "idle", "blink", "happy", "curious", "sleepy", "peek",
    "walk_right_1", "walk_right_2", "walk_left_1", "walk_left_2",
    "notify", "hover",
]
# Frames that intentionally sit lower than the rest (no top-headroom rule).
LOW_FRAMES = {"sleepy", "peek"}


def _parse_hex(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _chroma_key(im: Image.Image, rgb: tuple[int, int, int], tol: int) -> Image.Image:
    """Make pixels close to `rgb` fully transparent."""
    px = im.load()
    w, h = im.size
    tr, tg, tb = rgb
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
                px[x, y] = (r, g, b, 0)
    return im


def _binarize_alpha(im: Image.Image, thresh: int) -> Image.Image:
    r, g, b, a = im.split()
    a = a.point(lambda v: 255 if v >= thresh else 0)
    return Image.merge("RGBA", (r, g, b, a))


def _content(im: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    bbox = im.getchannel("A").getbbox()
    if bbox is None:
        raise SystemExit("a frame is fully transparent after cleanup")
    return im.crop(bbox), bbox


def load_raw(name: str) -> Image.Image | None:
    p = SRC / f"{name}.png"
    if not p.exists():
        return None
    return Image.open(p).convert("RGBA")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=96)
    ap.add_argument("--top", type=int, default=10, help="min headroom above the head")
    ap.add_argument("--bottom", type=int, default=4, help="gap below the feet")
    ap.add_argument("--alpha-thresh", type=int, default=128)
    ap.add_argument("--bg", default=None, help="flat background colour to key out, e.g. #FF00FF")
    ap.add_argument("--bg-tol", type=int, default=24)
    ap.add_argument("--mirror-walk", action="store_true",
                    help="derive walk_left_* by horizontally flipping walk_right_*")
    ap.add_argument("--out", default="moonshell_96")
    args = ap.parse_args()

    out_dir = ROOT / "assets" / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    bg_rgb = _parse_hex(args.bg) if args.bg else None

    # ---- preprocess every available frame to a clean, trimmed RGBA ----
    cleaned: dict[str, tuple[Image.Image, tuple[int, int, int, int]]] = {}
    for name in FRAMES:
        im = load_raw(name)
        if im is None:
            continue
        if bg_rgb is not None:
            im = _chroma_key(im, bg_rgb, args.bg_tol)
        im = _binarize_alpha(im, args.alpha_thresh)
        cleaned[name] = _content(im)

    if args.mirror_walk:
        for r, l in (("walk_right_1", "walk_left_1"), ("walk_right_2", "walk_left_2")):
            if r in cleaned and l not in cleaned:
                img, box = cleaned[r]
                cleaned[l] = (img.transpose(Image.FLIP_LEFT_RIGHT), box)

    if not cleaned:
        raise SystemExit(f"no input PNGs found in {SRC} (expected names: {', '.join(FRAMES)})")

    missing = [n for n in FRAMES if n not in cleaned]
    if missing:
        print("[warn] missing frames (will be skipped):", ", ".join(missing))

    # ---- one global scale, derived from the tallest "normal" frame (prefer idle) ----
    content_h = args.target - args.top - args.bottom
    ref = "idle" if "idle" in cleaned else max(
        (n for n in cleaned if n not in LOW_FRAMES),
        key=lambda n: cleaned[n][0].height,
        default=next(iter(cleaned)),
    )
    ref_h = cleaned[ref][0].height
    scale = content_h / ref_h
    print(f"[scale] reference frame={ref} content_h={ref_h} -> scale={scale:.4f}")

    # ---- compose each frame: scale, bottom-anchor, center ----
    for name, (img, _box) in cleaned.items():
        nw = max(1, round(img.width * scale))
        nh = max(1, round(img.height * scale))
        # never let an unusually wide frame exceed the canvas
        if nw > args.target - 4:
            k = (args.target - 4) / nw
            nw = round(nw * k)
            nh = round(nh * k)
        scaled = img.resize((nw, nh), Image.NEAREST)

        canvas = Image.new("RGBA", (args.target, args.target), (0, 0, 0, 0))
        x = (args.target - nw) // 2
        y = args.target - args.bottom - nh
        y = max(args.top if name not in LOW_FRAMES else 0, y)
        canvas.alpha_composite(scaled, (x, y))
        canvas.save(out_dir / f"{name}.png")

    # ---- contact sheet for eyeballing ----
    have = [n for n in FRAMES if n in cleaned]
    Z, cols = 4, 4
    cell = args.target * Z + 20
    rows = (len(have) + cols - 1) // cols
    sheet = Image.new("RGBA", (cell * cols, cell * rows), (245, 245, 250, 255))
    for i, name in enumerate(have):
        im = Image.open(out_dir / f"{name}.png").resize((args.target * Z, args.target * Z), Image.NEAREST)
        ox = (i % cols) * cell + 10
        oy = (i // cols) * cell + 10
        sheet.alpha_composite(im, (ox, oy))
    PREVIEW.parent.mkdir(exist_ok=True)
    sheet.convert("RGB").save(PREVIEW)

    print(f"[done] wrote {len(have)} frames to {out_dir}")
    print(f"[done] preview -> {PREVIEW}")
    print("Review the preview; if good, tell me and I'll swap them into assets/moonshell/ "
          "and bump the code from 48px to the new size.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
