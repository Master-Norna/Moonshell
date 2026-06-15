"""Extract the 12 frames from a baked-checkerboard sprite sheet (docs/image1.png).

The sheet has no real alpha: the transparency checker is painted into RGB. So we
rebuild alpha by flood-filling the light-grey checker inward from the borders
(interior white glints stay opaque because they aren't connected to the border),
then slice the 4x3 grid by the now-transparent gaps and save named frames into
assets/_incoming/ for tools/ingest_sprites.py to normalize.
"""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "_incoming"

# reading order, 4 columns x 3 rows
FRAME_GRID = [
    "idle", "blink", "happy", "curious",
    "sleepy", "peek", "walk_right_1", "walk_right_2",
    "walk_left_1", "walk_left_2", "notify", "hover",
]
COLS, ROWS = 4, 3


def is_checker(r: int, g: int, b: int) -> bool:
    """Light, low-saturation pixel = part of the grey transparency checker."""
    mx, mn = max(r, g, b), min(r, g, b)
    sat = mx - mn
    return mx > 110 and sat < 70


def build_alpha(im: Image.Image) -> Image.Image:
    w, h = im.size
    px = im.load()
    bg = bytearray(w * h)  # 1 = background
    dq: deque[tuple[int, int]] = deque()

    def consider(x: int, y: int) -> None:
        i = y * w + x
        if bg[i]:
            return
        r, g, b = px[x, y][:3]
        if is_checker(r, g, b):
            bg[i] = 1
            dq.append((x, y))

    for x in range(w):
        consider(x, 0)
        consider(x, h - 1)
    for y in range(h):
        consider(0, y)
        consider(w - 1, y)

    while dq:
        x, y = dq.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h:
                consider(nx, ny)

    alpha = Image.new("L", (w, h), 0)
    ap = alpha.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            ap[x, y] = 0 if bg[row + x] else 255
    return alpha


def zero_run_cuts(proj: list[int], n_seg: int) -> list[int]:
    """Cut points between n_seg content bands, taken at the midpoints of the
    interior all-zero runs. Falls back to an even split if detection is off."""
    w = len(proj)
    runs: list[tuple[int, int]] = []
    s = None
    for i, v in enumerate(proj + [1]):
        if v == 0 and s is None:
            s = i
        elif v != 0 and s is not None:
            runs.append((s, i - 1))
            s = None
    interior = [r for r in runs if r[0] > 0 and r[1] < w - 1]
    interior.sort(key=lambda r: r[1] - r[0], reverse=True)
    cuts = sorted((a + b) // 2 for a, b in interior[: n_seg - 1])
    if len(cuts) != n_seg - 1:
        cuts = [round(w * k / n_seg) for k in range(1, n_seg)]
    return cuts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", default="image1.png")
    args = ap.parse_args()

    src = ROOT / "docs" / args.sheet
    im = Image.open(src).convert("RGB")
    w, h = im.size
    alpha = build_alpha(im)
    rgba = im.convert("RGBA")
    rgba.putalpha(alpha)

    ap_px = alpha.load()
    colproj = [sum(1 for y in range(h) if ap_px[x, y] > 0) for x in range(w)]
    rowproj = [sum(1 for x in range(w) if ap_px[x, y] > 0) for y in range(h)]
    xcuts = [0] + zero_run_cuts(colproj, COLS) + [w]
    ycuts = [0] + zero_run_cuts(rowproj, ROWS) + [h]
    print("column cuts:", xcuts)
    print("row cuts:", ycuts)

    OUT.mkdir(parents=True, exist_ok=True)
    idx = 0
    for r in range(ROWS):
        for c in range(COLS):
            name = FRAME_GRID[idx]
            idx += 1
            cell = rgba.crop((xcuts[c], ycuts[r], xcuts[c + 1], ycuts[r + 1]))
            bbox = cell.getchannel("A").getbbox()
            if bbox is None:
                print(f"[warn] {name}: empty cell")
                continue
            cell.crop(bbox).save(OUT / f"{name}.png")
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            print(f"[ok] {name:14s} -> {bw}x{bh}")

    print(f"\nwrote frames to {OUT}")
    print("next: python tools/ingest_sprites.py --target 96")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
