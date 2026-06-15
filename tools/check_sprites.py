from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pet.sprite_config import OPTIONAL_SPRITES, REQUIRED_SPRITES, SPRITE_SIZE

SPRITE_DIR = ROOT / "assets" / "moonshell"


def _check_one(name: str) -> bool:
    path = SPRITE_DIR / f"{name}.png"
    im = Image.open(path).convert("RGBA")
    alpha_hist = im.getchannel("A").histogram()
    alpha_values = [i for i, n in enumerate(alpha_hist) if n]
    valid = im.size == (SPRITE_SIZE, SPRITE_SIZE) and set(alpha_values) <= {0, 255}
    state = "OK" if valid else "FAIL"
    print(f"[{state}] {name:13s} size={im.size} bbox={im.getbbox()} "
          f"alpha={alpha_values[:4]}{'...' if len(alpha_values) > 4 else ''}")
    return valid


def main() -> int:
    print("[check] sprites:", SPRITE_DIR)
    ok = True
    for name in REQUIRED_SPRITES:
        if not (SPRITE_DIR / f"{name}.png").exists():
            print(f"[missing] {name}.png")
            ok = False
            continue
        ok = _check_one(name) and ok

    present_optional = [n for n in OPTIONAL_SPRITES if (SPRITE_DIR / f"{n}.png").exists()]
    if present_optional:
        print(f"[check] optional action poses ({len(present_optional)}):")
        for name in present_optional:
            ok = _check_one(name) and ok

    if not ok:
        raise SystemExit(1)
    print(f"[check] sprites OK: all {SPRITE_SIZE}x{SPRITE_SIZE} with clean alpha.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
