from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pet.sprite_config import ACTIVE_SPRITES, RETIRED_SPRITES, SPRITE_SIZE

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
    for name in ACTIVE_SPRITES:
        if not (SPRITE_DIR / f"{name}.png").exists():
            print(f"[missing] {name}.png")
            ok = False
            continue
        ok = _check_one(name) and ok

    present_names = {path.stem for path in SPRITE_DIR.glob("*.png")}
    retired_present = sorted(present_names.intersection(RETIRED_SPRITES))
    if retired_present:
        print(
            f"[check] ignored retired source frames ({len(retired_present)}): "
            + ", ".join(retired_present)
        )
    unclassified = sorted(
        present_names.difference(ACTIVE_SPRITES).difference(RETIRED_SPRITES)
    )
    if unclassified:
        print("[unclassified] " + ", ".join(f"{name}.png" for name in unclassified))
        ok = False

    if not ok:
        raise SystemExit(1)
    print(
        f"[check] {len(ACTIVE_SPRITES)} active sprites OK: "
        f"all {SPRITE_SIZE}x{SPRITE_SIZE} with clean alpha."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
