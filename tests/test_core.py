from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image

from main import _acquire_single_instance
from pet.monitor import machine_load
from pet.settings import Settings
from pet.sprite_config import REQUIRED_SPRITES, SPRITE_SIZE
from tools import check_sprites


class MachineLoadTests(unittest.TestCase):
    def test_uses_hottest_compute_device(self) -> None:
        self.assertAlmostEqual(machine_load(10, 50, 90), 0.84)

    def test_clamps_invalid_percentages(self) -> None:
        self.assertEqual(machine_load(200, -5, None), 0.85)


class SettingsTests(unittest.TestCase):
    def test_round_trip_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            expected = Settings(
                size_mode="standard",
                x=120,
                screen_name="DISPLAY1",
                x_ratio=0.25,
                activity="low",
            )
            expected.save(path)
            self.assertEqual(Settings.load(path), expected)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_invalid_values_fall_back_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text(
                '{"size_mode":"huge","x":"bad","x_ratio":4,"activity":"berserk"}',
                encoding="utf-8",
            )
            loaded = Settings.load(path)
            self.assertEqual(loaded.size_mode, "small")
            self.assertIsNone(loaded.x)
            self.assertIsNone(loaded.x_ratio)
            self.assertEqual(loaded.activity, "high")

    def test_legacy_toggles_migrate_to_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            for payload, expected in (
                ('{"quiet_mode": true}', "low"),
                ('{"wander": false}', "low"),
                ('{"wander": true}', "high"),
                ("{}", "high"),
            ):
                path.write_text(payload, encoding="utf-8")
                self.assertEqual(Settings.load(path).activity, expected)


class SpriteValidationTests(unittest.TestCase):
    def test_required_sprites_exist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in REQUIRED_SPRITES:
            self.assertTrue((root / "assets" / "moonshell" / f"{name}.png").exists())

    def test_semitransparent_alpha_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            old_dir = check_sprites.SPRITE_DIR
            try:
                check_sprites.SPRITE_DIR = Path(temp)
                image = Image.new("RGBA", (SPRITE_SIZE, SPRITE_SIZE), (1, 2, 3, 128))
                image.save(Path(temp) / "bad.png")
                with redirect_stdout(io.StringIO()):
                    self.assertFalse(check_sprites._check_one("bad"))
            finally:
                check_sprites.SPRITE_DIR = old_dir


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected(self) -> None:
        name = f"MoonShellSpirit-test-{uuid.uuid4().hex}"
        script = (
            "import sys\n"
            "from PySide6.QtCore import QCoreApplication, QTimer\n"
            "from main import _acquire_single_instance\n"
            "app = QCoreApplication([])\n"
            "guard = _acquire_single_instance(sys.argv[1])\n"
            "if guard is None:\n"
            "    raise SystemExit(2)\n"
            "print('ready', flush=True)\n"
            "QTimer.singleShot(1500, app.quit)\n"
            "app.exec()\n"
            "guard.close()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertIsNone(_acquire_single_instance(name))
        finally:
            child.wait(timeout=3)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()


if __name__ == "__main__":
    unittest.main()
