from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import main as app_main
from main import _acquire_single_instance
from pet import paths as app_paths
from pet.logging_setup import configure_logging
from pet.monitor import _MonitorWorker, machine_load
from pet.settings import Settings
from pet.sprite_config import OPTIONAL_SPRITES, REQUIRED_SPRITES, SPRITE_SIZE
from pet.state import PetState
from pet.version import APP_VERSION
from tools.check_build_environment import check_environment, parse_lock
from tools.check_build_provenance import check_provenance
from tools.check_release import (
    _check_recorded_build_environment,
    _check_release_input_hashes,
    _check_stable_source_record,
)
from tools.package_release import package_release
from tools.release_inputs import release_input_paths
from tools.source_provenance import collect_source_provenance
from tools.write_build_info import write_build_info
from tools import check_sprites


class MachineLoadTests(unittest.TestCase):
    def test_uses_hottest_compute_device(self) -> None:
        self.assertAlmostEqual(machine_load(10, 50, 90), 0.84)

    def test_clamps_invalid_percentages(self) -> None:
        self.assertEqual(machine_load(200, -5, None), 0.85)
        self.assertEqual(machine_load(float("nan"), float("inf"), None), 0.0)


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
                system_awareness=False,
                clipboard_reactions=True,
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

    def test_one_malformed_field_does_not_discard_valid_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text(
                '{"enabled":false,"always_on_top":false,"size_mode":[],'
                '"activity":{},"x":true,"x_ratio":NaN,'
                '"system_awareness":"yes","clipboard_reactions":1}',
                encoding="utf-8",
            )
            loaded = Settings.load(path)
            self.assertFalse(loaded.enabled)
            self.assertFalse(loaded.always_on_top)
            self.assertEqual(loaded.size_mode, "small")
            self.assertEqual(loaded.activity, "high")
            self.assertIsNone(loaded.x)
            self.assertIsNone(loaded.x_ratio)
            self.assertTrue(loaded.system_awareness)
            self.assertFalse(loaded.clipboard_reactions)

    def test_huge_number_only_resets_its_own_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            path.write_text(
                '{"enabled":false,"x_ratio":' + "9" * 500 + "}",
                encoding="utf-8",
            )
            loaded = Settings.load(path)
            self.assertFalse(loaded.enabled)
            self.assertIsNone(loaded.x_ratio)

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


class DataPathTests(unittest.TestCase):
    def test_legacy_data_migration_is_atomic_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "legacy"
            current = root / "current"
            legacy.mkdir()
            (legacy / "settings.json").write_text(
                '{"activity":"low"}',
                encoding="utf-8",
            )
            (legacy / "state.json").write_text(
                '{"moon_tokens":77}',
                encoding="utf-8",
            )
            real_atomic_write = app_paths.atomic_write_json
            failed_once = False

            def fail_first_state(path: Path, payload: dict) -> None:
                nonlocal failed_once
                if path.name == "state.json" and not failed_once:
                    failed_once = True
                    raise OSError("transient state copy failure")
                real_atomic_write(path, payload)

            with (
                patch.object(app_paths, "LEGACY_DATA_DIR", legacy),
                patch.object(app_paths, "DATA_DIR", current),
                patch.object(app_paths, "USING_DATA_DIR_OVERRIDE", False),
                patch.object(
                    app_paths,
                    "atomic_write_json",
                    side_effect=fail_first_state,
                ),
                self.assertLogs("pet.paths", level="WARNING"),
            ):
                first = app_paths.migrate_legacy_data()
            self.assertEqual([path.name for path in first], ["settings.json"])
            self.assertFalse((current / app_paths.MIGRATION_MARKER).exists())
            self.assertFalse((current / "settings.json.tmp").exists())

            # Simulate the interrupted launch saving fresh defaults. Because no
            # migration marker exists, the next launch must still recover the
            # older companion memory instead of trusting this default file.
            real_atomic_write(current / "state.json", {"moon_tokens": 0})
            with (
                patch.object(app_paths, "LEGACY_DATA_DIR", legacy),
                patch.object(app_paths, "DATA_DIR", current),
                patch.object(app_paths, "USING_DATA_DIR_OVERRIDE", False),
            ):
                second = app_paths.migrate_legacy_data()
            self.assertEqual(
                {path.name for path in second},
                {"settings.json", "state.json"},
            )
            self.assertEqual(
                app_paths.read_json_object(current / "state.json")["moon_tokens"],
                77,
            )
            self.assertTrue((current / app_paths.MIGRATION_MARKER).exists())

    def test_clear_local_data_removes_only_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            legacy = root / "legacy"
            current.mkdir()
            legacy.mkdir()
            for directory in (current, legacy):
                (directory / "settings.json").write_text("{}", encoding="utf-8")
                (directory / "state.json").write_text("{}", encoding="utf-8")
                (directory / "keep-me.txt").write_text("user", encoding="utf-8")
            (current / app_paths.MIGRATION_MARKER).write_text(
                "{}",
                encoding="utf-8",
            )

            with (
                patch.object(app_paths, "DATA_DIR", current),
                patch.object(app_paths, "LEGACY_DATA_DIR", legacy),
            ):
                failures = app_paths.clear_known_local_data(include_legacy=True)
            self.assertEqual(failures, [])
            for directory in (current, legacy):
                self.assertFalse((directory / "settings.json").exists())
                self.assertFalse((directory / "state.json").exists())
                self.assertTrue((directory / "keep-me.txt").exists())

    def test_partial_legacy_clear_leaves_a_non_personal_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "current"
            legacy = root / "legacy"
            current.mkdir()
            legacy.mkdir()
            (current / "state.json").write_text("{}", encoding="utf-8")
            legacy_state = legacy / "state.json"
            legacy_state.write_text('{"moon_tokens":99}', encoding="utf-8")
            real_unlink = Path.unlink

            def occupied_unlink(path: Path, *args, **kwargs) -> None:
                if path == legacy_state:
                    raise PermissionError("occupied")
                real_unlink(path, *args, **kwargs)

            with (
                patch.object(app_paths, "DATA_DIR", current),
                patch.object(app_paths, "LEGACY_DATA_DIR", legacy),
                patch.object(Path, "unlink", occupied_unlink),
            ):
                failures = app_paths.clear_known_local_data(include_legacy=True)
            self.assertEqual([path for path, _ in failures], [legacy_state])
            self.assertTrue(legacy_state.exists())
            tombstone = app_paths.read_json_object(
                current / app_paths.MIGRATION_MARKER
            )
            self.assertTrue(tombstone["legacy_migration_blocked_after_clear"])


class SpriteValidationTests(unittest.TestCase):
    def test_required_sprites_exist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in REQUIRED_SPRITES:
            self.assertTrue((root / "assets" / "moonshell" / f"{name}.png").exists())

    def test_published_optional_sprites_are_complete(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sprite_dir = root / "assets" / "moonshell"
        expected = set(REQUIRED_SPRITES) | set(OPTIONAL_SPRITES)
        actual = {path.stem for path in sprite_dir.glob("*.png")}
        missing = [
            name
            for name in OPTIONAL_SPRITES
            if not (sprite_dir / f"{name}.png").exists()
        ]
        self.assertEqual(missing, [])
        self.assertEqual(actual, expected)

    def test_published_sprites_share_a_24_color_palette(self) -> None:
        root = Path(__file__).resolve().parents[1]
        colors: set[tuple[int, int, int]] = set()
        for name in (*REQUIRED_SPRITES, *OPTIONAL_SPRITES):
            with Image.open(root / "assets" / "moonshell" / f"{name}.png") as image:
                rgba = image.convert("RGBA")
                pixels = (
                    rgba.get_flattened_data()
                    if hasattr(rgba, "get_flattened_data")
                    else rgba.getdata()
                )
                colors.update(
                    (red, green, blue)
                    for red, green, blue, alpha in pixels
                    if alpha
                )
        self.assertEqual(len(colors), 24)

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


class PetStateTests(unittest.TestCase):
    def test_round_trip_includes_daily_gift_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            expected = PetState(
                last_seen=1.0,
                energy=0.2,
                mood=0.8,
                sleepiness=0.4,
                first_seen_date="2026-07-01",
                last_gift_date="2026-07-25",
                moon_tokens=12,
                focus_until=2_000_000_000.0,
            )
            expected.save(path)
            loaded = PetState.load(path)
            self.assertAlmostEqual(loaded.energy, 0.2)
            self.assertAlmostEqual(loaded.mood, 0.8)
            self.assertAlmostEqual(loaded.sleepiness, 0.4)
            self.assertEqual(loaded.first_seen_date, "2026-07-01")
            self.assertEqual(loaded.last_gift_date, "2026-07-25")
            self.assertEqual(loaded.moon_tokens, 12)
            self.assertEqual(loaded.focus_until, 2_000_000_000.0)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_invalid_values_are_finite_and_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(
                '{"last_seen":"bad","energy":true,"mood":NaN,'
                '"sleepiness":4,"first_seen_date":"tomorrow",'
                '"last_gift_date":"2026-99-99","moon_tokens":true,'
                '"focus_until":-5}',
                encoding="utf-8",
            )
            loaded = PetState.load(path)
            self.assertEqual(loaded.last_seen, 0.0)
            self.assertEqual(loaded.energy, 0.6)
            self.assertEqual(loaded.mood, 0.6)
            self.assertEqual(loaded.sleepiness, 1.0)
            self.assertEqual(loaded.first_seen_date, "")
            self.assertEqual(loaded.last_gift_date, "")
            self.assertEqual(loaded.moon_tokens, 0)
            self.assertEqual(loaded.focus_until, 0.0)

    def test_companion_journal_fields_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            today_key = date.today().isoformat()
            expected = PetState(
                focus_planned_minutes=50,
                focus_sessions_completed=12,
                focus_minutes_completed=425,
                focus_today_date=today_key,
                focus_today_minutes=75,
                last_moon_event_key="-42",
            )

            self.assertTrue(expected.save(path))
            loaded = PetState.load(path)

            self.assertEqual(loaded.focus_planned_minutes, 50)
            self.assertEqual(loaded.focus_sessions_completed, 12)
            self.assertEqual(loaded.focus_minutes_completed, 425)
            self.assertEqual(loaded.focus_today_date, today_key)
            self.assertEqual(loaded.focus_today_minutes, 75)
            self.assertEqual(loaded.last_moon_event_key, "-42")

    def test_old_state_gets_safe_companion_journal_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(
                '{"energy":0.25,"moon_tokens":8,"focus_until":0}',
                encoding="utf-8",
            )

            loaded = PetState.load(path)

            self.assertAlmostEqual(loaded.energy, 0.25)
            self.assertEqual(loaded.moon_tokens, 8)
            self.assertEqual(loaded.focus_planned_minutes, 0)
            self.assertEqual(loaded.focus_sessions_completed, 0)
            self.assertEqual(loaded.focus_minutes_completed, 0)
            self.assertEqual(loaded.focus_today_date, "")
            self.assertEqual(loaded.focus_today_minutes, 0)
            self.assertEqual(loaded.last_moon_event_key, "")

    def test_invalid_companion_journal_values_only_reset_their_own_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(
                '{"moon_tokens":9,"focus_planned_minutes":true,'
                '"focus_sessions_completed":-4,'
                '"focus_minutes_completed":"many",'
                '"focus_today_date":"not-a-date",'
                '"focus_today_minutes":false,'
                '"last_moon_event_key":"full-moon"}',
                encoding="utf-8",
            )

            loaded = PetState.load(path)

            self.assertEqual(loaded.moon_tokens, 9)
            self.assertEqual(loaded.focus_planned_minutes, 0)
            self.assertEqual(loaded.focus_sessions_completed, 0)
            self.assertEqual(loaded.focus_minutes_completed, 0)
            self.assertEqual(loaded.focus_today_date, "")
            self.assertEqual(loaded.focus_today_minutes, 0)
            self.assertEqual(loaded.last_moon_event_key, "")

    def test_huge_companion_journal_values_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            huge = "9" * 200
            path.write_text(
                '{"focus_planned_minutes":' + huge + ","
                '"focus_sessions_completed":' + huge + ","
                '"focus_minutes_completed":' + huge + ","
                f'"focus_today_date":"{date.today().isoformat()}",'
                '"focus_today_minutes":' + huge + ","
                '"last_moon_event_key":"' + huge + '"}',
                encoding="utf-8",
            )

            loaded = PetState.load(path)

            self.assertEqual(loaded.focus_planned_minutes, 90)
            self.assertGreaterEqual(loaded.focus_sessions_completed, 0)
            self.assertLessEqual(loaded.focus_sessions_completed, 1_000_000)
            self.assertGreaterEqual(loaded.focus_minutes_completed, 0)
            self.assertLessEqual(loaded.focus_minutes_completed, 10_000_000)
            self.assertGreaterEqual(loaded.focus_today_minutes, 0)
            self.assertLessEqual(loaded.focus_today_minutes, 1_440)
            self.assertEqual(loaded.last_moon_event_key, "")

    def test_future_focus_today_date_is_repaired_without_losing_minutes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            today = date.today()
            path.write_text(
                (
                    '{"focus_today_date":"'
                    + (today + timedelta(days=1)).isoformat()
                    + '","focus_today_minutes":75}'
                ),
                encoding="utf-8",
            )

            loaded = PetState.load(path)
            self.assertTrue(loaded.normalize_focus_today(today))

            self.assertEqual(loaded.focus_today_date, today.isoformat())
            self.assertEqual(loaded.focus_today_minutes, 75)

    def test_stale_focus_today_date_resets_only_today_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            today = date.today()
            path.write_text(
                (
                    '{"focus_sessions_completed":5,'
                    '"focus_minutes_completed":180,'
                    '"focus_today_date":"'
                    + (today - timedelta(days=1)).isoformat()
                    + '","focus_today_minutes":75}'
                ),
                encoding="utf-8",
            )

            loaded = PetState.load(path)
            self.assertTrue(loaded.normalize_focus_today(today))

            self.assertEqual(loaded.focus_today_date, today.isoformat())
            self.assertEqual(loaded.focus_today_minutes, 0)
            self.assertEqual(loaded.focus_sessions_completed, 5)
            self.assertEqual(loaded.focus_minutes_completed, 180)

    def test_companionship_days_has_no_streak_penalty(self) -> None:
        state = PetState(first_seen_date="2026-07-20")
        self.assertEqual(state.companionship_days(date(2026, 7, 25)), 6)
        self.assertEqual(PetState().companionship_days(date(2026, 7, 25)), 1)
        self.assertEqual(
            PetState(first_seen_date="2026-08-01").companionship_days(
                date(2026, 7, 25)
            ),
            1,
        )

    def test_huge_number_only_resets_its_own_state_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(
                '{"energy":0.2,"mood":' + "9" * 500 + "}",
                encoding="utf-8",
            )
            loaded = PetState.load(path)
            self.assertEqual(loaded.energy, 0.2)
            self.assertEqual(loaded.mood, 0.6)

    def test_save_failure_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            blocker = Path(temp) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            with self.assertLogs("pet.state", level="WARNING"):
                saved = PetState().save(blocker / "state.json")
            self.assertFalse(saved)


class MonitorTests(unittest.TestCase):
    def test_inactive_worker_does_not_take_startup_sample(self) -> None:
        worker = _MonitorWorker(active=False)
        with patch.object(worker, "_tick") as tick:
            worker.start()
        self.assertIsNotNone(worker.timer)
        assert worker.timer is not None
        self.assertFalse(worker.timer.isActive())
        tick.assert_not_called()

    def test_sensor_failure_does_not_drop_the_rest_of_snapshot(self) -> None:
        worker = _MonitorWorker(active=True)
        samples = []
        worker.telemetry.connect(samples.append)
        memory = MagicMock(percent=42.0)
        with (
            patch("pet.monitor.psutil.cpu_percent", side_effect=RuntimeError("cpu")),
            patch("pet.monitor.psutil.virtual_memory", return_value=memory),
            patch("pet.monitor.psutil.sensors_battery", return_value=None),
            patch.object(worker, "_read_gpu", return_value=(None, None, False)),
            patch("pet.monitor.system_idle_seconds", return_value=12.0),
        ):
            worker._tick()
        self.assertEqual(len(samples), 1)
        self.assertFalse(samples[0].cpu_sampled)
        self.assertTrue(samples[0].mem_sampled)
        self.assertEqual(samples[0].cpu, 0.0)
        self.assertEqual(samples[0].mem, 42.0)
        self.assertEqual(samples[0].idle_seconds, 12.0)

    def test_gpu_failure_clears_stale_sample_and_backs_off(self) -> None:
        worker = _MonitorWorker(active=False)
        worker._gpu_last = 99.0
        worker._gpu_memory_last = 88.0
        with (
            patch("pet.monitor.sys.platform", "win32"),
            patch(
                "pet.monitor.subprocess.run",
                side_effect=FileNotFoundError("nvidia-smi"),
            ) as run,
            self.assertLogs("pet.monitor", level="INFO"),
        ):
            for _ in range(3):
                worker._ticks = 3
                result = worker._read_gpu()
            self.assertEqual(result, (None, None, False))
            self.assertGreater(worker._gpu_retry_at, 0.0)
            calls = run.call_count
            self.assertEqual(worker._read_gpu(), (None, None, False))
            self.assertEqual(run.call_count, calls)

    def test_app_quit_shuts_down_monitor_thread(self) -> None:
        script = (
            "from PySide6.QtCore import QCoreApplication, QTimer\n"
            "from pet.monitor import SystemMonitor\n"
            "app = QCoreApplication([])\n"
            "monitor = SystemMonitor(active=False)\n"
            "QTimer.singleShot(20, app.quit)\n"
            "app.exec()\n"
            "raise SystemExit(0 if not monitor._thread.isRunning() else 3)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class SingleInstanceTests(unittest.TestCase):
    def test_smoke_namespace_is_stable_across_processes(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MOONSHELL_SMOKE_TEST": "1",
                "MOONSHELL_SMOKE_NAMESPACE": "release-probe",
            },
            clear=False,
        ):
            with patch.object(app_main.os, "getpid", return_value=100):
                first = app_main._instance_name()
            with patch.object(app_main.os, "getpid", return_value=200):
                second = app_main._instance_name()
        self.assertEqual(first, second)

    def test_unnamespaced_smoke_processes_remain_isolated(self) -> None:
        with patch.dict(
            os.environ,
            {"MOONSHELL_SMOKE_TEST": "1"},
            clear=False,
        ):
            os.environ.pop("MOONSHELL_SMOKE_NAMESPACE", None)
            with patch.object(app_main.os, "getpid", return_value=100):
                first = app_main._instance_name()
            with patch.object(app_main.os, "getpid", return_value=200):
                second = app_main._instance_name()
        self.assertNotEqual(first, second)

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


class BuildProvenanceTests(unittest.TestCase):
    def test_sources_under_explicit_roots_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            toc = root / "Analysis-00.toc"
            toc.write_text(
                repr(([], [("safe.dll", str(root / "safe.dll"), "BINARY")])),
                encoding="utf-8",
            )
            self.assertEqual(check_provenance(toc, allowed_roots=(root,)), [])

    def test_external_and_known_contaminating_dlls_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root.parent / "host-software" / "libcrypto-3-x64.dll"
            toc = root / "Analysis-00.toc"
            toc.write_text(
                repr(
                    (
                        [],
                        [
                            (
                                "libcrypto-3-x64.dll",
                                str(outside),
                                "BINARY",
                            )
                        ],
                    )
                ),
                encoding="utf-8",
            )
            failures = check_provenance(toc, allowed_roots=(root,))
            self.assertTrue(any("forbidden binary" in item for item in failures))
            self.assertTrue(any("not allowlisted" in item for item in failures))


class BuildEnvironmentTests(unittest.TestCase):
    def test_hashed_exact_locks_are_parsed_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "requirements-build.txt"
            lock.write_text(
                "Demo_Package==1.2.3 \\\n"
                f"    --hash=sha256:{'a' * 64}\n",
                encoding="utf-8",
            )
            packages, failures = parse_lock(lock)
            self.assertEqual(packages, {"demo-package": "1.2.3"})
            self.assertEqual(failures, [])

    def test_missing_hash_version_drift_and_extras_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "requirements-build.txt"
            lock.write_text(
                "expected==2.0 "
                f"--hash=sha256:{'b' * 64}\n"
                "unhashed==1.0 --only-binary=:all:\n",
                encoding="utf-8",
            )
            failures = check_environment(
                lock,
                installed={"expected": "1.9", "surprise": "4.0"},
            )
            self.assertTrue(any("no SHA-256" in item for item in failures))
            self.assertTrue(any("version mismatch" in item for item in failures))
            self.assertTrue(any("unexpected package" in item for item in failures))


class ReleaseContractTests(unittest.TestCase):
    @staticmethod
    def _git_query(
        commit: str,
        *,
        head_ok: bool = True,
        status_ok: bool = True,
        status: str = "",
        tag_ok: bool = True,
        main_ok: bool = True,
        main_target: str | None = None,
        source_on_main: bool = True,
    ):
        resolved_main = commit if main_target is None else main_target

        def query(_root: Path, *arguments: str) -> tuple[bool, str]:
            if arguments == ("rev-parse", "--verify", "HEAD"):
                return head_ok, commit if head_ok else ""
            if arguments == (
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main^{commit}",
            ):
                return main_ok, resolved_main if main_ok else ""
            if arguments == (
                "rev-parse",
                "--verify",
                f"refs/tags/v{APP_VERSION}^{{commit}}",
            ):
                return tag_ok, commit if tag_ok else ""
            if arguments[:2] == ("merge-base", "--is-ancestor"):
                return source_on_main, ""
            if arguments[0] == "status":
                return status_ok, status if status_ok else ""
            raise AssertionError(arguments)

        return query

    def test_exact_github_version_tag_is_release_eligible(self) -> None:
        commit = "e" * 40
        with patch(
            "tools.source_provenance._git_output",
            side_effect=self._git_query(commit),
        ):
            provenance = collect_source_provenance(
                Path("."),
                f"v{APP_VERSION}",
                environ={
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_SHA": commit,
                    "GITHUB_REF_TYPE": "tag",
                    "GITHUB_REF_NAME": f"v{APP_VERSION}",
                },
            )
        self.assertTrue(provenance["source_git_verified"])
        self.assertFalse(provenance["source_dirty"])
        self.assertTrue(provenance["release_eligible"])
        self.assertEqual(provenance["source_tag_target"], commit)
        self.assertTrue(provenance["source_on_main"])
        self.assertEqual(provenance["source_main_target"], commit)

    def test_git_query_failure_is_never_treated_as_release_eligible(self) -> None:
        commit = "a" * 40

        def git_query(_root: Path, *arguments: str) -> tuple[bool, str]:
            if arguments[:2] == ("rev-parse", "--verify"):
                return True, commit
            if arguments[:2] == ("merge-base", "--is-ancestor"):
                return True, ""
            if arguments[0] == "status":
                return False, ""
            if arguments[:2] == ("tag", "--points-at"):
                return True, f"v{APP_VERSION}"
            raise AssertionError(arguments)

        with patch(
            "tools.source_provenance._git_output",
            side_effect=git_query,
        ):
            provenance = collect_source_provenance(
                Path("."),
                f"v{APP_VERSION}",
                environ={
                    "GITHUB_SHA": commit,
                    "GITHUB_REF_TYPE": "tag",
                    "GITHUB_REF_NAME": f"v{APP_VERSION}",
                },
            )
        self.assertTrue(provenance["source_dirty"])
        self.assertFalse(provenance["source_git_verified"])
        self.assertFalse(provenance["release_eligible"])

    def test_all_provenance_mismatches_fail_closed(self) -> None:
        commit = "f" * 40
        expected_tag = f"v{APP_VERSION}"
        scenarios = (
            ("head failure", {"head_ok": False}, {}),
            ("tag failure", {"tag_ok": False}, {}),
            ("main query failure", {"main_ok": False}, {}),
            ("commit outside main", {"source_on_main": False}, {}),
            ("event SHA mismatch", {}, {"GITHUB_SHA": "0" * 40}),
            ("wrong event tag", {}, {"GITHUB_REF_NAME": "v9.9.9"}),
        )
        for label, git_options, environment_overrides in scenarios:
            with self.subTest(label=label):
                environment = {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_SHA": commit,
                    "GITHUB_REF_TYPE": "tag",
                    "GITHUB_REF_NAME": expected_tag,
                    **environment_overrides,
                }
                with patch(
                    "tools.source_provenance._git_output",
                    side_effect=self._git_query(commit, **git_options),
                ):
                    provenance = collect_source_provenance(
                        Path("."),
                        expected_tag,
                        environ=environment,
                    )
                self.assertFalse(provenance["release_eligible"])

    def test_tracked_and_untracked_changes_remain_qa_eligible(self) -> None:
        commit = "1" * 40
        for status in (" M pet/pet_window.py", "?? local.txt"):
            with self.subTest(status=status):
                with patch(
                    "tools.source_provenance._git_output",
                    side_effect=self._git_query(commit, status=status),
                ):
                    provenance = collect_source_provenance(
                        Path("."),
                        f"v{APP_VERSION}",
                        environ={},
                    )
                self.assertTrue(provenance["source_git_verified"])
                self.assertTrue(provenance["source_dirty"])
                self.assertFalse(provenance["release_eligible"])

    def test_release_tag_remains_eligible_after_main_advances(self) -> None:
        commit = "1" * 40
        main_target = "2" * 40
        with patch(
            "tools.source_provenance._git_output",
            side_effect=self._git_query(
                commit,
                main_target=main_target,
                source_on_main=True,
            ),
        ):
            provenance = collect_source_provenance(
                Path("."),
                f"v{APP_VERSION}",
                environ={},
            )
        self.assertTrue(provenance["source_on_main"])
        self.assertEqual(provenance["source_main_target"], main_target)
        self.assertTrue(provenance["release_eligible"])

    def test_release_tag_is_rejected_after_main_drops_its_commit(self) -> None:
        with patch(
            "tools.source_provenance._git_output",
            side_effect=self._git_query(
                "3" * 40,
                main_target="4" * 40,
                source_on_main=False,
            ),
        ):
            provenance = collect_source_provenance(
                Path("."),
                f"v{APP_VERSION}",
                environ={},
            )
        self.assertFalse(provenance["source_on_main"])
        self.assertFalse(provenance["release_eligible"])

    def test_clean_source_requires_the_exact_version_tag(self) -> None:
        commit = "b" * 40

        def git_query(_root: Path, *arguments: str) -> tuple[bool, str]:
            if arguments == ("rev-parse", "--verify", "HEAD"):
                return True, commit
            if arguments[:2] == ("rev-parse", "--verify"):
                return False, ""
            if arguments[0] == "status":
                return True, ""
            raise AssertionError(arguments)

        with patch(
            "tools.source_provenance._git_output",
            side_effect=git_query,
        ):
            provenance = collect_source_provenance(
                Path("."),
                f"v{APP_VERSION}",
                environ={},
            )
        self.assertEqual(provenance["source_tag"], "")
        self.assertFalse(provenance["release_eligible"])

    def test_github_branch_at_tagged_commit_remains_qa_only(self) -> None:
        commit = "d" * 40

        def git_query(_root: Path, *arguments: str) -> tuple[bool, str]:
            if arguments[:2] == ("rev-parse", "--verify"):
                return True, commit
            if arguments[:2] == ("merge-base", "--is-ancestor"):
                return True, ""
            if arguments[0] == "status":
                return True, ""
            raise AssertionError(arguments)

        with patch(
            "tools.source_provenance._git_output",
            side_effect=git_query,
        ):
            provenance = collect_source_provenance(
                Path("."),
                f"v{APP_VERSION}",
                environ={
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_SHA": commit,
                    "GITHUB_REF_TYPE": "branch",
                    "GITHUB_REF_NAME": "main",
                },
            )
        self.assertEqual(provenance["source_tag"], f"v{APP_VERSION}")
        self.assertFalse(provenance["source_git_verified"])
        self.assertFalse(provenance["release_eligible"])

    def test_build_info_records_actual_environment_not_only_lock_copy(self) -> None:
        expected_lock = {
            "pyside6-essentials": "6.11.1",
            "shiboken6": "6.11.1",
            "psutil": "7.2.2",
            "pillow": "12.3.0",
            "pyinstaller": "6.21.0",
            "pyinstaller-hooks-contrib": "2026.6",
        }
        runtime = {
            "PySide6-Essentials": "6.11.1",
            "shiboken6": "6.11.1",
            "psutil": "7.2.2",
        }
        build = {
            "Pillow": "12.3.0",
            "PyInstaller": "6.21.0",
            "pyinstaller-hooks-contrib": "2026.6",
        }
        build_info: dict[str, object] = {
            "locked_build_environment": expected_lock,
            "actual_build_environment": dict(expected_lock),
            "runtime_packages": runtime,
            "build_packages": build,
            "packages": {**runtime, **build},
        }
        self.assertEqual(
            _check_recorded_build_environment(build_info, expected_lock),
            [],
        )
        build_info["actual_build_environment"] = {
            **expected_lock,
            "pillow": "12.2.0",
        }
        self.assertTrue(
            any(
                "actual build environment" in failure
                for failure in _check_recorded_build_environment(
                    build_info,
                    expected_lock,
                )
            )
        )
        build_info["actual_build_environment"] = {
            package: version
            for package, version in expected_lock.items()
            if package != "pillow"
        }
        self.assertTrue(
            any(
                "actual build environment" in failure
                for failure in _check_recorded_build_environment(
                    build_info,
                    expected_lock,
                )
            )
        )

    def test_build_info_writer_rejects_environment_drift(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "tools.write_build_info.check_environment",
                return_value=["locked package is not installed: pillow"],
            ),
            patch(
                "tools.write_build_info.importlib.metadata.version",
                side_effect=AssertionError(
                    "package versions must not be queried after validation fails"
                ),
            ) as version,
        ):
            with self.assertRaisesRegex(ValueError, "pillow"):
                write_build_info(Path(temp) / "BUILD_INFO.json")
        version.assert_not_called()

    def test_release_input_hash_set_must_be_complete_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
            second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
            with patch(
                "tools.check_release.release_input_paths",
                return_value=(first, second),
            ):
                incomplete = _check_release_input_hashes(
                    {"release_input_sha256": {"first.txt": first_hash}},
                    root=root,
                )
                complete = _check_release_input_hashes(
                    {
                        "release_input_sha256": {
                            "first.txt": first_hash,
                            "second.txt": second_hash,
                        }
                    },
                    root=root,
                )
            self.assertTrue(any("hash set mismatch" in item for item in incomplete))
            self.assertEqual(complete, [])
            with patch(
                "tools.check_release.release_input_paths",
                return_value=(first, second),
            ):
                extra = _check_release_input_hashes(
                    {
                        "release_input_sha256": {
                            "first.txt": first_hash,
                            "second.txt": second_hash,
                            "extra.txt": "a" * 64,
                        }
                    },
                    root=root,
                )
                malformed = _check_release_input_hashes(
                    {
                        "release_input_sha256": {
                            "first.txt": first_hash.upper(),
                            "second.txt": second_hash,
                        }
                    },
                    root=root,
                )
                first.write_text("tampered", encoding="utf-8")
                tampered = _check_release_input_hashes(
                    {
                        "release_input_sha256": {
                            "first.txt": first_hash,
                            "second.txt": second_hash,
                        }
                    },
                    root=root,
                )
            self.assertTrue(any("hash set mismatch" in item for item in extra))
            self.assertTrue(any("first.txt" in item for item in malformed))
            self.assertTrue(any("first.txt" in item for item in tampered))

    def test_release_inputs_exclude_ignored_art_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = (
                root / "assets" / "_incoming" / "raw.png",
                root / "docs" / "image.png",
                root / "docs" / "daily-card-preview.png",
                root / "docs" / "preview.png",
                root / "assets" / "moonshell" / "idle.png",
            )
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"test")
            relatives = {
                path.relative_to(root).as_posix()
                for path in release_input_paths(root)
            }
            self.assertNotIn("assets/_incoming/raw.png", relatives)
            self.assertNotIn("docs/image.png", relatives)
            self.assertIn("docs/daily-card-preview.png", relatives)
            self.assertIn("docs/preview.png", relatives)
            self.assertIn("assets/moonshell/idle.png", relatives)

    def test_public_record_and_current_checkout_must_share_version_tag(self) -> None:
        commit = "c" * 40
        expected_tag = f"v{APP_VERSION}"
        record: dict[str, object] = {
            "source_dirty": False,
            "source_git_verified": True,
            "release_eligible": True,
            "source_commit": commit,
            "source_tag": expected_tag,
            "source_tag_target": commit,
            "source_main_target": commit,
            "source_on_main": True,
        }
        current = {
            **record,
        }
        self.assertEqual(
            _check_stable_source_record(
                record,
                current_provenance=current,
            ),
            [],
        )
        record["source_tag"] = ""
        self.assertTrue(
            any(
                "tag must exactly match" in item
                for item in _check_stable_source_record(
                    record,
                    current_provenance=current,
                )
            )
        )

class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.workflow = (
            root
            / ".github"
            / "workflows"
            / "release.yml"
        ).read_text(encoding="utf-8")
        cls.tests_workflow = (
            root
            / ".github"
            / "workflows"
            / "tests.yml"
        ).read_text(encoding="utf-8")
        cls.readme = (root / "README.md").read_text(encoding="utf-8")

    def _job(self, name: str) -> str:
        marker = f"  {name}:\n"
        start = self.workflow.index(marker)
        following = re.search(
            r"^  [a-zA-Z0-9_-]+:\n",
            self.workflow[start + len(marker) :],
            flags=re.MULTILINE,
        )
        if following is None:
            return self.workflow[start:]
        end = start + len(marker) + following.start()
        return self.workflow[start:end]

    def test_release_jobs_keep_build_read_only_and_publish_unsigned(self) -> None:
        build = self._job("build")
        publish = self._job("publish")
        self.assertIn("contents: read", build)
        self.assertNotIn("contents: write", build)
        self.assertIn("needs: build", publish)
        self.assertIn("contents: write", publish)
        for forbidden in (
            "sign-and-package",
            "SIGNING_CERTIFICATE",
            "EXPECTED_SIGNER",
            "name: production",
            "--stable",
        ):
            self.assertNotIn(forbidden, self.workflow)
        self.assertIn("tools/package_release.py --unsigned-release", build)
        self.assertIn("tools/package_release.py --qa", build)
        self.assertIn(
            "if: github.event_name == 'workflow_dispatch'",
            build,
        )
        self.assertIn(
            "MoonShell-windows-x64-unsigned-qa-${{ github.sha }}",
            build,
        )
        self.assertIn(
            "MoonShell-windows-x64-maintenance-${{ github.sha }}",
            build,
        )
        self.assertIn(
            "dist/MoonShell-*-windows-x64-portable.zip",
            build,
        )

    def test_release_rechecks_main_tag_checksum_and_unsigned_status(self) -> None:
        self.assertIn("git merge-base --is-ancestor", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn(
            "github.event_name == 'push' && github.ref_type == 'tag'",
            self.workflow,
        )
        self.assertIn('"${{ github.event_name }}" -eq "push"', self.workflow)
        self.assertIn("Manual workflow runs never publish", self.workflow)
        publish = self._job("publish")
        self.assertIn("commits/$env:GITHUB_REF_NAME", publish)
        self.assertIn("$RemoteTag -ne $EventSha", publish)
        self.assertIn("compare/$EventSha...$RemoteMain", publish)
        self.assertIn("Get-FileHash", publish)
        self.assertIn("--verify-tag", publish)
        self.assertIn("GH_REPO: ${{ github.repository }}", publish)
        self.assertIn("未使用 Authenticode", publish)
        self.assertIn(
            "MoonShell-$Version-windows-x64-portable.zip",
            publish,
        )
        self.assertIn(
            "SHA-256 校验不等同于发布者代码签名",
            publish,
        )
        self.assertIn("SmartScreen", publish)

    def test_public_filename_and_readme_images_stay_consistent(self) -> None:
        public_name = "MoonShell-<版本>-windows-x64-portable.zip"
        self.assertIn(public_name, self.readme)
        self.assertIn(
            r".\MoonShell-<版本>-windows-x64-portable.zip",
            self.readme,
        )
        self.assertNotIn("unsigned-maintenance.zip", self.readme)
        self.assertIn("docs/preview.png", self.readme)
        self.assertIn("docs/daily-card-preview.png", self.readme)
        self.assertIn("没有 Authenticode 代码签名", self.readme)
        self.assertIn("SmartScreen", self.readme)

    def test_dependency_audits_do_not_install_windows_lock_on_linux(self) -> None:
        for workflow in (self.workflow, self.tests_workflow):
            with self.subTest(workflow=workflow.splitlines()[0]):
                self.assertIn(
                    "internal-be-careful-extra-flags: --disable-pip",
                    workflow,
                )
                self.assertNotRegex(
                    workflow,
                    r"(?m)^\s+disable-pip:",
                )

    def test_release_summary_writes_are_not_split_redirections(self) -> None:
        self.assertNotIn(">>", self.workflow)
        self.assertGreaterEqual(
            self.workflow.count(
                "Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY "
                "-Encoding utf8 -Value @("
            ),
            2,
        )

    def test_pe_validation_and_packaging_are_separate_steps(self) -> None:
        self.assertIn("- name: Validate PE metadata", self.workflow)
        self.assertIn("- name: Package Windows release", self.workflow)
        self.assertNotIn(
            "- name: Validate PE metadata and package",
            self.workflow,
        )

    def test_release_actions_are_pinned_to_commit_hashes(self) -> None:
        revisions = re.findall(
            r"^\s*uses:\s+[^@\s]+@([^\s]+)",
            self.workflow,
            flags=re.MULTILINE,
        )
        self.assertTrue(revisions)
        for revision in revisions:
            with self.subTest(revision=revision):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")


class ReleaseArchiveTests(unittest.TestCase):
    @staticmethod
    def _fake_distribution(root: Path) -> Path:
        distribution = root / "MoonShell"
        executable = distribution / "MoonShell.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"MZ" + b"\0" * (1024 * 1024))
        plugin = distribution / "_internal" / "platforms" / "qwindows.dll"
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"plugin")
        sprite = (
            distribution
            / "_internal"
            / "assets"
            / "moonshell"
            / "idle.png"
        )
        sprite.parent.mkdir(parents=True)
        sprite.write_bytes(b"sprite")
        return executable

    def _assert_qa_state_preserved(
        self,
        root: Path,
        qa_archive: Path,
        qa_bytes: bytes,
        manifest_bytes: bytes,
    ) -> None:
        maintenance_archive = (
            root
            / (
                f"MoonShell-{APP_VERSION}-windows-x64"
                "-portable.zip"
            )
        )
        self.assertEqual(qa_archive.read_bytes(), qa_bytes)
        self.assertEqual(
            (root / "SHA256SUMS.txt").read_bytes(),
            manifest_bytes,
        )
        self.assertFalse(maintenance_archive.exists())
        self.assertEqual(list(root.glob("*.tmp")), [])
        self.assertEqual(list(root.glob("*.bak")), [])

    def test_qa_archive_is_labeled_and_public_mode_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self._fake_distribution(root)

            qa_archive, _ = package_release(executable, root)
            self.assertTrue(qa_archive.name.endswith("-unsigned-qa.zip"))
            with zipfile.ZipFile(qa_archive) as bundle:
                notice = f"MoonShell-{APP_VERSION}/UNSIGNED_BUILD.txt"
                self.assertIn(notice, bundle.namelist())
                self.assertIn("unsigned QA", bundle.read(notice).decode("utf-8"))

            with self.assertRaisesRegex(ValueError, "Select exactly one"):
                package_release(executable, root, qa=False)
            self.assertTrue(qa_archive.exists())

            with patch(
                "tools.package_release._unsigned_release_failures",
                return_value=[],
            ):
                public_archive, _ = package_release(
                    executable,
                    root,
                    qa=False,
                    unsigned_release=True,
                )
            self.assertFalse(qa_archive.exists())
            self.assertTrue(public_archive.exists())
            with zipfile.ZipFile(public_archive) as bundle:
                self.assertNotIn(
                    f"MoonShell-{APP_VERSION}/UNSIGNED_BUILD.txt",
                    bundle.namelist(),
                )
                self.assertIn(
                    f"MoonShell-{APP_VERSION}/UNSIGNED_RELEASE.txt",
                    bundle.namelist(),
                )

    def test_unsigned_public_release_requires_clean_tag_and_keeps_notice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self._fake_distribution(root)
            qa_archive, _ = package_release(executable, root)

            with self.assertRaisesRegex(
                ValueError,
                "Unsigned release policy failed",
            ):
                package_release(
                    executable,
                    root,
                    qa=False,
                    unsigned_release=True,
                )
            self.assertTrue(qa_archive.exists())

            with patch(
                "tools.package_release._unsigned_release_failures",
                return_value=[],
            ):
                archive, manifest = package_release(
                    executable,
                    root,
                    qa=False,
                    unsigned_release=True,
                )
            self.assertEqual(
                archive.name,
                (
                    f"MoonShell-{APP_VERSION}-windows-x64"
                    "-portable.zip"
                ),
            )
            self.assertFalse(qa_archive.exists())
            manifest_parts = (
                manifest.read_text(encoding="ascii").strip().split("  ", 1)
            )
            self.assertEqual(manifest_parts[1], archive.name)
            self.assertEqual(
                manifest_parts[0],
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
            with zipfile.ZipFile(archive) as bundle:
                folder = f"MoonShell-{APP_VERSION}"
                names = set(bundle.namelist())
                self.assertIn(f"{folder}/README.md", names)
                self.assertIn(f"{folder}/docs/preview.png", names)
                self.assertIn(
                    f"{folder}/docs/daily-card-preview.png",
                    names,
                )
                packaged_readme = bundle.read(
                    f"{folder}/README.md"
                ).decode("utf-8")
                self.assertIn("docs/preview.png", packaged_readme)
                self.assertIn(
                    "docs/daily-card-preview.png",
                    packaged_readme,
                )
                notice = f"MoonShell-{APP_VERSION}/UNSIGNED_RELEASE.txt"
                text = bundle.read(notice).decode("utf-8")
            self.assertIn("not Authenticode-signed", text)
            self.assertIn("SHA256SUMS.txt", text)
            self.assertIn("does not authenticate the publisher", text)

    def test_hash_failure_preserves_qa_archive_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self._fake_distribution(root)
            qa_archive, manifest = package_release(executable, root)
            qa_bytes = qa_archive.read_bytes()
            manifest_bytes = manifest.read_bytes()

            with (
                patch(
                    "tools.package_release._unsigned_release_failures",
                    return_value=[],
                ),
                patch(
                    "tools.package_release._sha256",
                    side_effect=OSError("injected hash failure"),
                ),
                self.assertRaisesRegex(OSError, "injected hash failure"),
            ):
                package_release(
                    executable,
                    root,
                    qa=False,
                    unsigned_release=True,
                )

            self._assert_qa_state_preserved(
                root,
                qa_archive,
                qa_bytes,
                manifest_bytes,
            )

    def test_manifest_commit_failure_rolls_back_release_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self._fake_distribution(root)
            qa_archive, manifest = package_release(executable, root)
            qa_bytes = qa_archive.read_bytes()
            manifest_bytes = manifest.read_bytes()
            real_replace = os.replace
            failure_injected = False

            def fail_manifest_commit(source: object, target: object) -> None:
                nonlocal failure_injected
                source_path = Path(source)
                target_path = Path(target)
                if (
                    not failure_injected
                    and source_path.name == "SHA256SUMS.txt.tmp"
                    and target_path.name == "SHA256SUMS.txt"
                ):
                    failure_injected = True
                    raise OSError("injected manifest commit failure")
                real_replace(source, target)

            with (
                patch(
                    "tools.package_release._unsigned_release_failures",
                    return_value=[],
                ),
                patch(
                    "tools.package_release.os.replace",
                    side_effect=fail_manifest_commit,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "injected manifest commit failure",
                ),
            ):
                package_release(
                    executable,
                    root,
                    qa=False,
                    unsigned_release=True,
                )

            self.assertTrue(failure_injected)
            self._assert_qa_state_preserved(
                root,
                qa_archive,
                qa_bytes,
                manifest_bytes,
            )

    def test_archive_commit_failure_rolls_back_release_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = self._fake_distribution(root)
            qa_archive, manifest = package_release(executable, root)
            qa_bytes = qa_archive.read_bytes()
            manifest_bytes = manifest.read_bytes()
            maintenance_name = (
                f"MoonShell-{APP_VERSION}-windows-x64"
                "-portable.zip"
            )
            real_replace = os.replace
            failure_injected = False

            def fail_archive_commit(source: object, target: object) -> None:
                nonlocal failure_injected
                source_path = Path(source)
                target_path = Path(target)
                if (
                    not failure_injected
                    and source_path.name == f"{maintenance_name}.tmp"
                    and target_path.name == maintenance_name
                ):
                    failure_injected = True
                    raise OSError("injected archive commit failure")
                real_replace(source, target)

            with (
                patch(
                    "tools.package_release._unsigned_release_failures",
                    return_value=[],
                ),
                patch(
                    "tools.package_release.os.replace",
                    side_effect=fail_archive_commit,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "injected archive commit failure",
                ),
            ):
                package_release(
                    executable,
                    root,
                    qa=False,
                    unsigned_release=True,
                )

            self.assertTrue(failure_injected)
            self._assert_qa_state_preserved(
                root,
                qa_archive,
                qa_bytes,
                manifest_bytes,
            )


class LoggingTests(unittest.TestCase):
    def test_release_log_is_written_to_the_local_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with (
                patch("pet.logging_setup.logging.basicConfig") as basic_config,
                patch("pet.logging_setup.sys.stderr", None),
            ):
                log_path = configure_logging(Path(temp))
                self.assertEqual(log_path, Path(temp) / "moonshell.log")
                handlers = basic_config.call_args.kwargs["handlers"]
                record = logging.LogRecord(
                    "test.release",
                    logging.ERROR,
                    __file__,
                    1,
                    "release-log-probe",
                    (),
                    None,
                )
                for handler in handlers:
                    handler.emit(record)
                    handler.flush()
                    handler.close()
            assert log_path is not None
            self.assertIn(
                "release-log-probe",
                log_path.read_text(encoding="utf-8"),
            )


class ApplicationStartupTests(unittest.TestCase):
    def test_release_smoke_timeout_is_explicit_and_bounded(self) -> None:
        with patch.dict(
            os.environ,
            {"MOONSHELL_SMOKE_TEST": "1", "MOONSHELL_SMOKE_EXIT_MS": "1200"},
            clear=False,
        ):
            self.assertEqual(app_main._smoke_exit_delay_ms(), 1200)
        with patch.dict(
            os.environ,
            {"MOONSHELL_SMOKE_TEST": "1", "MOONSHELL_SMOKE_EXIT_MS": "99999"},
            clear=False,
        ):
            self.assertEqual(app_main._smoke_exit_delay_ms(), 0)
        with patch.dict(
            os.environ,
            {"MOONSHELL_SMOKE_EXIT_MS": "1200"},
            clear=True,
        ):
            self.assertEqual(app_main._smoke_exit_delay_ms(), 0)

    def test_disabled_setting_is_not_overridden_by_main(self) -> None:
        app = MagicMock()
        app.exec.return_value = 0
        guard = MagicMock()
        window = MagicMock()
        with (
            patch.object(app_main, "QApplication", return_value=app),
            patch.object(app_main.QGuiApplication, "setHighDpiScaleFactorRoundingPolicy"),
            patch.object(app_main, "_acquire_single_instance", return_value=guard),
            patch.object(app_main, "migrate_legacy_data", return_value=[]),
            patch.object(app_main.Settings, "load", return_value=Settings(enabled=False)),
            patch.object(app_main, "PixelPetWindow", return_value=window),
            patch.object(app_main, "configure_logging"),
            patch.object(app_main, "install_exception_hook"),
        ):
            self.assertEqual(app_main.main(), 0)
        window.show.assert_not_called()
        window._shutdown.assert_called_once()
        guard.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
