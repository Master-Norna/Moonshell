"""Prepare three generated feature poses for the existing pixel pipeline.

The image generator may return a near-white checkerboard baked into an RGB
sheet even when transparency was requested.  This tool flood-removes only the
edge-connected neutral background, aligns each pose to the canonical 96x96
stage, and writes a visual candidate sheet.  ``--apply`` updates smooth masters;
``tools/pixelize.py --apply`` remains the only way to publish live sprites.

Usage:
    python tools/prepare_feature_sprites.py --sheet path/to/sheet.png
    python tools/prepare_feature_sprites.py --sheet path/to/sheet.png --apply
"""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
MASTERS = ROOT / "assets" / "_masters"
LIVE = ROOT / "assets" / "moonshell"
PREVIEW = ROOT / "docs" / "feature_pose_candidate.png"
POSES = ("read", "magic", "star")
SIZE = 96
BASELINE = 91
TARGET_EXTENT = 82


def _neutral_background(pixel: tuple[int, int, int]) -> bool:
    """Recognize the generator's near-white neutral checker, not highlights."""
    red, green, blue = pixel
    return min(pixel) >= 235 and max(pixel) - min(pixel) <= 14


def _remove_edge_background(cell: Image.Image) -> Image.Image:
    rgb = cell.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    background = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()

    def seed(x: int, y: int) -> None:
        index = y * width + x
        if not background[index] and _neutral_background(pixels[x, y]):
            background[index] = 1
            queue.append((x, y))

    for x in range(width):
        seed(x, 0)
        seed(x, height - 1)
    for y in range(height):
        seed(0, y)
        seed(width - 1, y)

    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            index = ny * width + nx
            if background[index] or not _neutral_background(pixels[nx, ny]):
                continue
            background[index] = 1
            queue.append((nx, ny))

    rgba = rgb.convert("RGBA")
    alpha = Image.frombytes(
        "L",
        rgba.size,
        bytes(0 if value else 255 for value in background),
    )
    rgba.putalpha(alpha)
    return rgba


def _aligned_pose(cell: Image.Image) -> Image.Image:
    isolated = _remove_edge_background(cell)
    bounds = isolated.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("cell contains no foreground after background removal")
    cropped = isolated.crop(bounds)
    scale = min(TARGET_EXTENT / cropped.width, TARGET_EXTENT / cropped.height)
    target = (
        max(1, round(cropped.width * scale)),
        max(1, round(cropped.height * scale)),
    )
    resized = cropped.resize(target, Image.Resampling.LANCZOS)
    alpha = resized.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    resized.putalpha(alpha)

    frame = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    x = (SIZE - resized.width) // 2
    y = BASELINE - resized.height + 1
    frame.alpha_composite(resized, (x, y))
    return frame


def prepare(sheet: Image.Image) -> dict[str, Image.Image]:
    if sheet.width < 3 or sheet.height < 1:
        raise ValueError("sheet is too small")
    frames: dict[str, Image.Image] = {}
    for index, name in enumerate(POSES):
        left = round(index * sheet.width / 3)
        right = round((index + 1) * sheet.width / 3)
        frames[name] = _aligned_pose(sheet.crop((left, 0, right, sheet.height)))
    return frames


def _checker(size: tuple[int, int]) -> Image.Image:
    background = Image.new("RGBA", size, (28, 30, 55, 255))
    draw = ImageDraw.Draw(background)
    tile = 12
    for y in range(0, size[1], tile):
        for x in range(0, size[0], tile):
            if (x // tile + y // tile) % 2 == 0:
                draw.rectangle(
                    (x, y, x + tile - 1, y + tile - 1),
                    fill=(36, 39, 70, 255),
                )
    return background


def _write_preview(candidates: dict[str, Image.Image]) -> None:
    scale = 4
    cell = SIZE * scale
    gap = 24
    label_height = 26
    canvas = Image.new(
        "RGBA",
        (cell * 4 + gap * 5, (cell + label_height) * 3 + gap * 4),
        (9, 10, 27, 255),
    )
    draw = ImageDraw.Draw(canvas)
    rows = (
        ("canonical", {"idle": Image.open(MASTERS / "idle.png").convert("RGBA")}),
        ("current", {name: Image.open(MASTERS / f"{name}.png").convert("RGBA") for name in POSES}),
        ("candidate", candidates),
    )
    for row_index, (row_name, frames) in enumerate(rows):
        y = gap + row_index * (cell + label_height + gap)
        draw.text((gap, y + 4), row_name, fill=(255, 222, 135, 255))
        for column, (name, frame) in enumerate(frames.items(), start=1):
            x = gap + column * (cell + gap)
            background = _checker((cell, cell))
            background.alpha_composite(
                frame.resize((cell, cell), Image.Resampling.NEAREST)
            )
            canvas.alpha_composite(background, (x, y))
            draw.text((x, y + cell + 5), name, fill=(205, 210, 255, 255))
    PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(PREVIEW)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write aligned smooth candidates to assets/_masters",
    )
    args = parser.parse_args()
    sheet = Image.open(args.sheet).convert("RGB")
    candidates = prepare(sheet)
    _write_preview(candidates)
    print(f"candidate preview -> {PREVIEW}")
    if args.apply:
        for name, frame in candidates.items():
            frame.save(MASTERS / f"{name}.png")
        print("updated smooth masters: " + ", ".join(POSES))
        print("run tools/pixelize.py before publishing live sprites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
