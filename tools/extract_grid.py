"""Slice an N-col x M-row baked-checkerboard sheet into individual cells.

Unlike extract_sheet.py (which hard-maps the 12 core game frames), this dumps
every cell of a sheet to a folder as r{row}c{col}.png with alpha rebuilt, plus a
contact sheet, so new action art can be eyeballed and named before ingest.

Usage:
    python tools/extract_grid.py --sheet image2.png --out grid2
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

# reuse the proven checker-keying + cut detection
from extract_sheet import build_alpha, zero_run_cuts  # type: ignore

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--out", required=True, help="subfolder under assets/_incoming/")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=3)
    ap.add_argument("--even", action="store_true",
                    help="force an even grid split (use when effects bridge the "
                         "gaps between cells and content-gap detection fails)")
    args = ap.parse_args()

    src = ROOT / "docs" / args.sheet
    im = Image.open(src).convert("RGB")
    w, h = im.size
    alpha = build_alpha(im)
    rgba = im.convert("RGBA")
    rgba.putalpha(alpha)

    if args.even:
        xcuts = [round(w * k / args.cols) for k in range(args.cols + 1)]
        ycuts = [round(h * k / args.rows) for k in range(args.rows + 1)]
    else:
        ap_px = alpha.load()
        colproj = [sum(1 for y in range(h) if ap_px[x, y] > 0) for x in range(w)]
        rowproj = [sum(1 for x in range(w) if ap_px[x, y] > 0) for y in range(h)]
        xcuts = [0] + zero_run_cuts(colproj, args.cols) + [w]
        ycuts = [0] + zero_run_cuts(rowproj, args.rows) + [h]
    print("column cuts:", xcuts)
    print("row cuts:", ycuts)

    out_dir = ROOT / "assets" / "_incoming" / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    cells: list[tuple[str, Image.Image]] = []
    for r in range(args.rows):
        for c in range(args.cols):
            cell = rgba.crop((xcuts[c], ycuts[r], xcuts[c + 1], ycuts[r + 1]))
            bbox = cell.getchannel("A").getbbox()
            name = f"r{r}c{c}"
            if bbox is None:
                print(f"[warn] {name}: empty")
                continue
            trimmed = cell.crop(bbox)
            trimmed.save(out_dir / f"{name}.png")
            cells.append((name, trimmed))
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            print(f"[ok] {name} -> {bw}x{bh}")

    # contact sheet (label each cell with its r/c id)
    Z = 220
    pad = 14
    grid_cols = args.cols
    grid_rows = args.rows
    sheet = Image.new("RGBA", (grid_cols * Z, grid_rows * Z), (245, 245, 250, 255))
    for name, im_cell in cells:
        r = int(name[1])
        c = int(name[3])
        fit = im_cell.copy()
        fit.thumbnail((Z - pad, Z - pad), Image.NEAREST)
        ox = c * Z + (Z - fit.width) // 2
        oy = r * Z + (Z - fit.height) // 2
        sheet.alpha_composite(fit, (ox, oy))
    preview = ROOT / "docs" / f"_grid_{args.out}.png"
    sheet.convert("RGB").save(preview)
    print(f"\nwrote {len(cells)} cells to {out_dir}")
    print(f"preview -> {preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
