"""Normalize extra ACTION frames (from extract_grid.py output) to the 96x96 spec.

Each source sheet draws the character at one consistent size, so we scale every
cell of a sheet by ONE factor (derived from a clean standing reference cell set
to the same body height as the existing idle), then binarize alpha, bottom-anchor
and center on a 96x96 canvas -- exactly matching the live frames so new poses sit
at the same on-screen size and stand on the taskbar.

Writes to assets/_incoming/_actions_out/ plus a size-comparison contact sheet
(docs/_actions_preview.png) that includes the live idle for scale reference.
Review, then copy the approved frames into assets/moonshell/.

Usage:
    python tools/ingest_actions.py            # default plan below
    python tools/ingest_actions.py --tweak 1.05   # nudge global scale
"""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
INC = ROOT / "assets" / "_incoming"
LIVE = ROOT / "assets" / "moonshell"
OUT = INC / "_actions_out"

TARGET, TOP, BOTTOM = 96, 10, 4
BODY_H = TARGET - TOP - BOTTOM  # 82, same as the existing idle body height

# grid dir, clean standing reference cell, [(cell, out_name, is_low)]
PLAN = [
    dict(grid="grid_2", ref="r0c1", frames=[
        ("r0c0", "wave", False),
        ("r0c1", "shy", False),
        ("r0c3", "pout", False),
        ("r1c0", "sad", False),
        ("r1c1", "excited", False),
        ("r1c2", "love", False),
        ("r2c2", "surprised", False),
        ("r2c3", "happy", False),    # gentle closed-eyes smile -> replaces the old, blank-looking happy
    ]),
    dict(grid="grid_4", ref="r2c0", frames=[
        ("r0c3", "sleep", True),
        ("r1c2", "dizzy", False),
        ("r1c0", "sit", True),
    ]),
    # image3: the "cool" magic batch (effects span cells -> sliced with --even).
    dict(grid="grid_3", ref="r0c0", frames=[
        ("r1c3", "read", False),    # reading a book
        ("r0c1", "magic", False),   # casting, sparkle ring
        ("r1c2", "star", False),    # holding a glowing star (tall glow beam clips at top)
        ("r2c2", "flame", False),   # purple flame aura
        ("r0c3", "twirl", False),   # happy spin
        ("r2c1", "moon", False),    # riding the crescent moon
        ("r1c0", "dash", False),    # running with a motion trail
        ("r2c3", "poof", False),    # vanishing into a puff of smoke
    ]),
]

def _binarize(im: Image.Image, thresh: int = 128) -> Image.Image:
    r, g, b, a = im.split()
    a = a.point(lambda v: 255 if v >= thresh else 0)
    return Image.merge("RGBA", (r, g, b, a))


def _trim(im: Image.Image) -> Image.Image:
    box = im.getchannel("A").getbbox()
    return im.crop(box) if box else im


def _load(grid: str, cell: str) -> Image.Image:
    return _trim(_binarize(Image.open(INC / grid / f"{cell}.png").convert("RGBA")))


def _components(im: Image.Image) -> list[list[tuple[int, int]]]:
    """8-connected opaque regions, largest first."""
    w, h = im.size
    px = im.load()
    seen = bytearray(w * h)
    out: list[list[tuple[int, int]]] = []
    for y in range(h):
        for x in range(w):
            if px[x, y][3] > 0 and not seen[y * w + x]:
                q = deque([(x, y)])
                seen[y * w + x] = 1
                cells: list[tuple[int, int]] = []
                while q:
                    cx, cy = q.popleft()
                    cells.append((cx, cy))
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            nx, ny = cx + dx, cy + dy
                            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and px[nx, ny][3] > 0:
                                seen[ny * w + nx] = 1
                                q.append((nx, ny))
                out.append(cells)
    out.sort(key=len, reverse=True)
    return out


def _main_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of the CHARACTER (largest opaque region), ignoring stray
    fragments and tall effects -- this is what we scale and seat by, so a glow
    beam or an extraction-artifact speck can never throw off size or footing."""
    comps = _components(im)
    main = comps[0]
    xs = [p[0] for p in main]
    ys = [p[1] for p in main]
    return min(xs), min(ys), max(xs), max(ys)


def _despeckle(im: Image.Image) -> Image.Image:
    """Drop extraction junk: cool (purple/blue cloak) fragments detached from the
    body, and tiny far-orphan specks of any color.  Gold sparkles (the intended
    accent) are kept."""
    comps = _components(im)
    if len(comps) <= 1:
        return im
    main = comps[0]
    mx0 = min(p[0] for p in main); mx1 = max(p[0] for p in main)
    my0 = min(p[1] for p in main); my1 = max(p[1] for p in main)
    px = im.load()
    for c in comps[1:]:
        x0 = min(p[0] for p in c); x1 = max(p[0] for p in c)
        y0 = min(p[1] for p in c); y1 = max(p[1] for p in c)
        gx = max(0, x0 - mx1, mx0 - x1)
        gy = max(0, y0 - my1, my0 - y1)
        r = g = b = 0
        for (x, y) in c:
            p = px[x, y]; r += p[0]; g += p[1]; b += p[2]
        n = len(c)
        cool = not (r / n > 120 and r / n > b / n + 30)
        if (cool and (gx >= 3 or gy >= 3)) or (len(c) <= 3 and (gx >= 8 or gy >= 8)):
            for (x, y) in c:
                px[x, y] = (0, 0, 0, 0)
    return im


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tweak", type=float, default=1.0, help="global scale multiplier")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    produced: list[str] = []

    for grp in PLAN:
        grid_dir = INC / grp["grid"]
        if not grid_dir.exists():
            print(f"[skip] {grp['grid']}: no source cells -- run extract_grid first")
            continue
        ref = _load(grp["grid"], grp["ref"])
        # Scale by the reference CHARACTER's body height (its largest opaque region),
        # so a clean standing pose pins every other frame to the same on-screen size.
        _, ry0, _, ry1 = _main_bbox(ref)
        ref_body_h = ry1 - ry0 + 1
        scale = (BODY_H / ref_body_h) * args.tweak
        print(f"[{grp['grid']}] ref={grp['ref']} body_h={ref_body_h} -> scale={scale:.4f}")
        for cell, name, low in grp["frames"]:
            img = _load(grp["grid"], cell)
            nw = max(1, round(img.width * scale))
            nh = max(1, round(img.height * scale))
            if nw > TARGET - 4:  # never wider than the canvas
                k = (TARGET - 4) / nw
                nw, nh = round(nw * k), round(nh * k)
            scaled = img.resize((max(1, nw), max(1, nh)), Image.NEAREST)
            # Seat the CHARACTER's feet (main-component bottom) on idle's baseline,
            # measured AFTER scaling so it's exact.  A tall effect (e.g. star's glow
            # beam) simply clips at the canvas top instead of shrinking the figure --
            # the bug that made star tiny and clipped its hood.
            _, _, _, body_bottom = _main_bbox(scaled)
            canvas = Image.new("RGBA", (TARGET, TARGET), (0, 0, 0, 0))
            x = (TARGET - nw) // 2
            y = (TARGET - BOTTOM) - (body_bottom + 1)
            if low:
                y = max(0, y)  # crouched poses rest on the floor, never above it
            canvas.alpha_composite(scaled, (x, y))
            canvas = _despeckle(canvas)  # strip extraction junk, keep gold sparkles
            canvas.save(OUT / f"{name}.png")
            produced.append(name)
            print(f"   {name:10s} {img.width}x{img.height} -> {nw}x{nh}  feet@{y + body_bottom}")

    # comparison contact sheet (live idle first, for size reference)
    Z, cols = 3, 6
    cell_px = TARGET * Z + 16
    items = [("idle*", LIVE / "idle.png")] + [(n, OUT / f"{n}.png") for n in produced]
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGBA", (cell_px * cols, cell_px * rows), (236, 236, 244, 255))
    for i, (label, path) in enumerate(items):
        im = Image.open(path).resize((TARGET * Z, TARGET * Z), Image.NEAREST)
        ox = (i % cols) * cell_px + 8
        oy = (i // cols) * cell_px + 8
        # a faint ground line so seating is comparable
        sheet.alpha_composite(im, (ox, oy))
    prev = ROOT / "docs" / "_actions_preview.png"
    sheet.convert("RGB").save(prev)
    print(f"\nwrote {len(produced)} frames to {OUT}")
    print(f"preview -> {prev}  (first cell = live idle, for size match)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
