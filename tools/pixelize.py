"""Turn the smooth (painterly, downsampled) masters into limited-palette pixel art.

assets/_masters/ holds the 96x96 smooth frames (hard edges + binary alpha, but
shading is still smooth gradients).  This collapses the active product set into
a small SHARED palette, so every published pose reads as flat cel-shaded pixel
art *and* the indigo/gold/purple stay identical frame to frame (no per-frame
colour drift / flicker), then writes those frames to assets/moonshell/.

The masters are the durable source -- never edit assets/moonshell/ by hand; edit /
add a master and re-run this so the active set shares one palette. Retired source
frames stay in place but do not influence the palette or get rebuilt.

Default = preview only (writes a before/after contact sheet, touches nothing).
Use --apply to (re)build the live set from the masters.

Usage:
    python tools/pixelize.py                 # preview at default colours
    python tools/pixelize.py --colors 16     # try a chunkier palette
    python tools/pixelize.py --apply         # rebuild assets/moonshell/ from masters
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pet.sprite_config import ACTIVE_SPRITES

LIVE = ROOT / "assets" / "moonshell"
MASTERS = ROOT / "assets" / "_masters"
PREVIEW = ROOT / "docs" / "pixelize_preview.png"

ALPHA_T = 128


def _frames() -> list[Path]:
    return [MASTERS / f"{name}.png" for name in ACTIVE_SPRITES]


def _build_palette(paths: list[Path], colors: int) -> Image.Image:
    """One adaptive (median-cut) palette from every opaque pixel in the set."""
    opaque: list[tuple[int, int, int]] = []
    for p in paths:
        im = Image.open(p).convert("RGBA")
        pixels = (
            im.get_flattened_data()
            if hasattr(im, "get_flattened_data")
            else im.getdata()
        )
        opaque.extend(
            (r, g, b) for (r, g, b, a) in pixels if a >= ALPHA_T
        )
    strip = Image.new("RGB", (len(opaque), 1))
    strip.putdata(opaque)
    # MEDIANCUT + no dither -> clean flat colour bands, the pixel-art look
    return strip.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def _apply_palette(im: Image.Image, pal: Image.Image) -> Image.Image:
    """Map one frame onto the shared palette, keeping its binary alpha."""
    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A").point(lambda v: 255 if v >= ALPHA_T else 0)
    quant = rgba.convert("RGB").quantize(palette=pal, dither=Image.Dither.NONE)
    out = quant.convert("RGBA")
    out.putalpha(alpha)
    return out


def _checker(img: Image.Image, scale: int) -> Image.Image:
    big = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    bg = Image.new("RGBA", big.size, (46, 46, 54, 255))
    tile = 12
    for yy in range(0, big.height, tile):
        for xx in range(0, big.width, tile):
            if ((xx // tile) + (yy // tile)) % 2 == 0:
                for a in range(xx, min(xx + tile, big.width)):
                    for b in range(yy, min(yy + tile, big.height)):
                        bg.putpixel((a, b), (58, 58, 66, 255))
    bg.alpha_composite(big)
    return bg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--colors", type=int, default=24, help="palette size (fewer = chunkier)")
    ap.add_argument("--apply", action="store_true", help="convert assets/moonshell in place")
    ap.add_argument("--sample", default="idle,happy,read,magic,star,walk_right_4",
                    help="frames to show in the preview")
    args = ap.parse_args()

    paths = _frames()
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        print("missing active sprite masters: " + ", ".join(missing))
        return 1
    pal = _build_palette(paths, args.colors)
    print(f"built shared {args.colors}-colour palette from {len(paths)} frames")

    if args.apply:
        LIVE.mkdir(parents=True, exist_ok=True)
        for p in paths:
            _apply_palette(Image.open(p), pal).save(LIVE / p.name)
        print(f"rebuilt {len(paths)} frames in {LIVE} from masters")
        return 0

    # preview: before(master) / after for a few representative frames, app size + zoomed
    active_names = set(ACTIVE_SPRITES)
    wanted = [s.strip() for s in args.sample.split(",")]
    names = [
        name
        for name in wanted
        if name in active_names and (MASTERS / f"{name}.png").is_file()
    ] or [p.stem for p in paths[:6]]
    from PIL import ImageDraw
    rows = []
    for n in names:
        before = Image.open(MASTERS / f"{n}.png").convert("RGBA")
        after = _apply_palette(before, pal)
        rows.append((n, _checker(before, 3), _checker(after, 3), _checker(after, 6)))
    gap, lab = 12, 18
    cw3 = 96 * 3
    W = 80 + cw3 * 2 + 96 * 6 + gap * 4
    H = lab + sum(max(r[1].height, r[3].height) for r in rows) + gap * len(rows) + 6
    cv = Image.new("RGBA", (W, H), (24, 24, 28, 255))
    d = ImageDraw.Draw(cv)
    d.text((10, 2), f"  before            after ({args.colors}c)      after @6x", fill=(235, 235, 240, 255))
    y = lab + 4
    for n, b, a3, a6 in rows:
        d.text((8, y), n, fill=(230, 210, 120, 255))
        x = 78
        cv.alpha_composite(b, (x, y)); x += b.width + gap
        cv.alpha_composite(a3, (x, y)); x += a3.width + gap
        cv.alpha_composite(a6, (x, y))
        y += max(b.height, a6.height) + gap
    cv.convert("RGB").save(PREVIEW)
    print(f"preview -> {PREVIEW}  (no files changed; re-run with --apply to commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
