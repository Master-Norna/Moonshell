from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pet.sprite_config import (
    OPTIONAL_SPRITES,
    SPRITE_SIZE,
    SPRITE_X,
    SPRITE_Y,
    STAGE_SIZE,
)

ASSETS = ROOT / "assets" / "moonshell"
OUT = ROOT / "docs" / "v14_stage_preview.png"

PHYSICAL_SCALE = 2

FRAMES = [
    ("idle", 0),
    ("blink", 0),
    ("happy", -2),
    ("curious", 0),
    ("sleepy", 0),
    ("peek", 0),
    ("walk_right_1", 0),
    ("walk_right_2", 0),
    ("walk_left_1", 0),
    ("walk_left_2", 0),
    ("notify", -2),
    ("hover", -6),
]
# Optional expressive poses are appended when present so the preview shows the
# full action set without ever failing if some are absent.
FRAMES += [(n, 0) for n in OPTIONAL_SPRITES if (ASSETS / f"{n}.png").exists()]


def nearest_stage(name: str, y_offset: int) -> Image.Image:
    img = Image.open(ASSETS / f"{name}.png").convert("RGBA")
    stage = Image.new("RGBA", (STAGE_SIZE, STAGE_SIZE), (0, 0, 0, 0))
    stage.alpha_composite(img, (SPRITE_X, SPRITE_Y + y_offset))
    return stage.resize((STAGE_SIZE * PHYSICAL_SCALE, STAGE_SIZE * PHYSICAL_SCALE), Image.Resampling.NEAREST)


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    cell_w, cell_h = 340, 340
    cols = 4
    rows = (len(FRAMES) + cols - 1) // cols
    sheet = Image.new("RGBA", (cell_w * cols, cell_h * rows), (250, 250, 255, 255))
    draw = ImageDraw.Draw(sheet)

    for i, (name, y_offset) in enumerate(FRAMES):
        x = (i % cols) * cell_w
        y = (i // cols) * cell_h
        # checkerboard + safe stage outline
        for yy in range(y + 20, y + 20 + STAGE_SIZE * PHYSICAL_SCALE, 12):
            for xx in range(x + 20, x + 20 + STAGE_SIZE * PHYSICAL_SCALE, 12):
                c = (235, 235, 242, 255) if ((xx + yy) // 12) % 2 else (246, 246, 250, 255)
                draw.rectangle([xx, yy, xx + 11, yy + 11], fill=c)
        draw.rectangle(
            [x + 20, y + 20, x + 20 + STAGE_SIZE * PHYSICAL_SCALE - 1, y + 20 + STAGE_SIZE * PHYSICAL_SCALE - 1],
            outline=(255, 180, 0, 255),
        )
        sprite_rect = [
            x + 20 + SPRITE_X * PHYSICAL_SCALE,
            y + 20 + (SPRITE_Y + y_offset) * PHYSICAL_SCALE,
            x + 20 + (SPRITE_X + SPRITE_SIZE) * PHYSICAL_SCALE - 1,
            y + 20 + (SPRITE_Y + y_offset + SPRITE_SIZE) * PHYSICAL_SCALE - 1,
        ]
        draw.rectangle(sprite_rect, outline=(0, 180, 255, 255))
        stage = nearest_stage(name, y_offset)
        sheet.alpha_composite(stage, (x + 20, y + 20))
        draw.text((x + 20, y + 20 + STAGE_SIZE * PHYSICAL_SCALE + 8), f"{name}  y={y_offset}", fill=(30, 36, 70, 255))

    sheet.convert("RGB").save(OUT)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
