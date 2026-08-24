from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pet.sprite_config import (
    ACTIVE_SPRITES,
    LAYOUT_Y_OFFSETS,
    SPRITE_X,
    SPRITE_Y,
    STAGE_SIZE,
)

ASSETS = ROOT / "assets" / "moonshell"


def alpha_bbox(img: Image.Image):
    return img.getchannel("A").getbbox()


def compose_stage(name: str, y_offset: int) -> Image.Image:
    img = Image.open(ASSETS / f"{name}.png").convert("RGBA")
    stage = Image.new("RGBA", (STAGE_SIZE, STAGE_SIZE), (0, 0, 0, 0))
    stage.alpha_composite(img, (SPRITE_X, SPRITE_Y + y_offset))
    return stage


def main() -> int:
    print("== v14 stage layout check ==")
    worst_top_margin = 999
    worst_bottom_margin = 999
    worst_left_margin = 999
    worst_right_margin = 999

    for name in ACTIVE_SPRITES:
        for y_offset in LAYOUT_Y_OFFSETS:
            stage = compose_stage(name, y_offset)
            bbox = alpha_bbox(stage)
            if bbox is None:
                raise SystemExit(f"{name}: empty stage")
            left, top, right, bottom = bbox
            margins = {
                "left": left,
                "top": top,
                "right": STAGE_SIZE - right,
                "bottom": STAGE_SIZE - bottom,
            }
            if min(margins.values()) < 1:
                raise SystemExit(f"{name} offset {y_offset}: unsafe bbox={bbox}, margins={margins}")

            worst_left_margin = min(worst_left_margin, margins["left"])
            worst_top_margin = min(worst_top_margin, margins["top"])
            worst_right_margin = min(worst_right_margin, margins["right"])
            worst_bottom_margin = min(worst_bottom_margin, margins["bottom"])

    print(f"minimum left margin:   {worst_left_margin}px")
    print(f"minimum top margin:    {worst_top_margin}px")
    print(f"minimum right margin:  {worst_right_margin}px")
    print(f"minimum bottom margin: {worst_bottom_margin}px")
    print(f"OK: every frame stays inside the {STAGE_SIZE}x{STAGE_SIZE} padded action stage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
