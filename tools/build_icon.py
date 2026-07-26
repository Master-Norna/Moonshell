from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "moonshell" / "idle.png"
OUTPUT = ROOT / "assets" / "branding" / "moonshell.ico"
ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def build_icon(source: Path = SOURCE, output: Path = OUTPUT) -> Path:
    """Create a crisp multi-resolution Windows icon from the shipped idle pose."""
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    alpha_box = image.getchannel("A").getbbox()
    if alpha_box is None:
        raise ValueError(f"icon source has no visible pixels: {source}")

    visible = image.crop(alpha_box)
    side = max(visible.size) + 8
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(
        visible,
        ((side - visible.width) // 2, (side - visible.height) // 2),
    )

    frames = [
        square.resize((size, size), Image.Resampling.NEAREST)
        for size in ICON_SIZES
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(
        output,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
        append_images=frames[:-1],
    )
    return output


def main() -> int:
    output = build_icon()
    with Image.open(output) as icon:
        sizes = sorted(icon.ico.sizes())  # type: ignore[attr-defined]
    expected = {(size, size) for size in ICON_SIZES}
    if set(sizes) != expected:
        raise RuntimeError(f"icon sizes mismatch: expected={expected}, actual={sizes}")
    print(f"wrote {output} ({', '.join(str(width) for width, _ in sizes)}px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
