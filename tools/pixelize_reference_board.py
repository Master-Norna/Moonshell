"""Normalize an image-generated direction board before it enters project docs.

The image generator can bake a pale checkerboard into an RGB result.  This
tool removes only the edge-connected neutral background, reduces the board to
a lower logical grid, maps it to the same 24-colour palette as the Active
sprites, and scales it back with nearest-neighbour sampling.

Default mode writes an ignored preview.  Use ``--apply`` to replace the tracked
documentation board; the original generated sheet stays under ``art_requests``.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

try:  # package import in tests
    from tools.prepare_feature_sprites import _remove_edge_background
    from tools.pixelize import _apply_palette, _build_palette, _frames
except ModuleNotFoundError:  # direct ``python tools/...`` invocation
    from prepare_feature_sprites import _remove_edge_background
    from pixelize import _apply_palette, _build_palette, _frames


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "art_requests" / "reference" / "v2_character_anchor.png"
DEFAULT_OUTPUT = ROOT / "docs" / "v2_character_anchor.png"
PREVIEW = ROOT / "art_requests" / "reference" / "v2_character_anchor_pixelized.png"


def pixelize_board(
    source: Image.Image,
    palette: Image.Image,
    *,
    block_size: int = 4,
) -> Image.Image:
    """Return a transparent, palette-limited, nearest-neighbour board."""
    if block_size < 1:
        raise ValueError("block_size must be positive")
    if source.width % block_size or source.height % block_size:
        raise ValueError("board dimensions must be divisible by block_size")

    isolated = _remove_edge_background(source)
    logical_size = (source.width // block_size, source.height // block_size)
    # The generated board already contains coarse pixel-like clusters.  Point
    # sampling keeps its baked pale background from being blended back into the
    # silhouette as a bright anti-aliased fringe.
    logical = isolated.resize(logical_size, Image.Resampling.NEAREST)
    pixelized = _apply_palette(logical, palette)
    return pixelized.resize(source.size, Image.Resampling.NEAREST)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--colors", type=int, default=24)
    parser.add_argument("--block-size", type=int, default=4)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="replace the tracked documentation board instead of writing a preview",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"source image does not exist: {args.source}")
        return 1

    paths = _frames()
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        print("missing active sprite masters: " + ", ".join(missing))
        return 1

    palette = _build_palette(paths, args.colors)
    result = pixelize_board(
        Image.open(args.source),
        palette,
        block_size=args.block_size,
    )
    destination = args.output if args.apply else PREVIEW
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.save(destination)
    mode = "updated tracked board" if args.apply else "preview only"
    print(f"{mode}: {destination}")
    print(
        f"RGBA, transparent edge background, {args.colors} shared colours, "
        f"{args.block_size}x nearest-neighbour blocks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
