from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from tools.prepare_feature_sprites import BASELINE, POSES, SIZE, prepare
from tools.pixelize_reference_board import pixelize_board


class FeatureSpritePipelineTests(unittest.TestCase):
    def test_generated_checker_is_removed_and_frames_share_baseline(self) -> None:
        sheet = Image.new("RGB", (300, 120), (250, 250, 250))
        draw = ImageDraw.Draw(sheet)
        for index in range(3):
            left = index * 100
            draw.rectangle((left + 26, 18, left + 74, 108), fill=(20, 30, 90))
            draw.rectangle((left + 38, 35, left + 62, 58), fill=(245, 190, 55))
            # An enclosed neutral eye highlight must not be mistaken for the
            # edge-connected neutral checker background.
            draw.point((left + 48, 45), fill=(255, 255, 255))

        frames = prepare(sheet)

        self.assertEqual(tuple(frames), POSES)
        for frame in frames.values():
            self.assertEqual(frame.mode, "RGBA")
            self.assertEqual(frame.size, (SIZE, SIZE))
            bounds = frame.getchannel("A").getbbox()
            self.assertIsNotNone(bounds)
            assert bounds is not None
            self.assertEqual(bounds[3] - 1, BASELINE)
            alpha = frame.getchannel("A")
            pixels = (
                alpha.get_flattened_data()
                if hasattr(alpha, "get_flattened_data")
                else alpha.getdata()
            )
            self.assertEqual(set(pixels), {0, 255})

    def test_reference_board_is_transparent_palette_limited_pixel_art(self) -> None:
        board = Image.new("RGB", (32, 16), (250, 250, 250))
        draw = ImageDraw.Draw(board)
        for y in range(16):
            for x in range(32):
                if (x // 4 + y // 4) % 2:
                    board.putpixel((x, y), (240, 240, 240))
        draw.rectangle((8, 4, 23, 15), fill=(25, 45, 120))
        draw.rectangle((12, 7, 19, 11), fill=(250, 190, 60))

        palette_source = Image.new("RGB", (3, 1))
        palette_source.putdata(((5, 8, 30), (28, 52, 130), (250, 195, 65)))
        palette = palette_source.quantize(
            colors=3,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        )
        result = pixelize_board(board, palette, block_size=4)

        self.assertEqual(result.mode, "RGBA")
        self.assertEqual(result.size, board.size)
        alpha = result.getchannel("A")
        pixels = (
            alpha.get_flattened_data()
            if hasattr(alpha, "get_flattened_data")
            else alpha.getdata()
        )
        self.assertEqual(set(pixels), {0, 255})
        for top in range(0, result.height, 4):
            for left in range(0, result.width, 4):
                block = result.crop((left, top, left + 4, top + 4))
                block_pixels = (
                    block.get_flattened_data()
                    if hasattr(block, "get_flattened_data")
                    else block.getdata()
                )
                self.assertEqual(len(set(block_pixels)), 1)


if __name__ == "__main__":
    unittest.main()
