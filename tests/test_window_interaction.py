from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from pet.monitor import Telemetry
from pet.pet_window import SpritePetWindow
from pet.settings import Settings


class WindowInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)
        cls.window = SpritePetWindow(Settings(), Path(__file__).resolve().parents[1])
        cls.window.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.monitor.shutdown()
        cls.window.close()

    def test_outer_transparent_corner_is_outside_character(self) -> None:
        self.assertFalse(self.window._is_interactive_point(QPoint(1, 1)))

    def test_character_body_is_grabbable(self) -> None:
        point = self._character_point()
        self.assertTrue(self.window._is_interactive_point(point))

    def test_click_triggers_pet_interaction(self) -> None:
        point = self._character_point()
        global_point = self.window.mapToGlobal(point)
        before = self.window._pet_count
        self.window.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress,
            point,
            global_point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        ))
        self.window.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease,
            point,
            global_point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        ))
        self.assertEqual(self.window._pet_count, before + 1)
        self.assertFalse(self.window.dragging)
        self.assertFalse(self.window.held)

    def test_upward_drag_starts_throw_physics(self) -> None:
        self.window._snap_to_taskbar(initial=True)
        point = self._character_point()
        global_start = self.window.mapToGlobal(point)
        global_end = global_start + QPoint(40, -80)
        local_end = point + QPoint(40, -80)

        self.window.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress,
            point,
            global_start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        ))
        self.window.mouseMoveEvent(self._mouse_event(
            QEvent.Type.MouseMove,
            local_end,
            global_end,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        ))
        self.window.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease,
            local_end,
            global_end,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        ))
        self.assertTrue(self.window.falling)
        self.assertTrue(self.window.phys_timer.isActive())
        self.window.falling = False
        self.window.phys_timer.stop()
        self.window._snap_to_taskbar(initial=True)

    def test_click_on_transparent_padding_does_not_grab(self) -> None:
        point = QPoint(1, 1)
        self.assertFalse(self.window._is_interactive_point(point))
        before = self.window._pet_count
        self.window.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress,
            point,
            self.window.mapToGlobal(point),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        ))
        self.assertFalse(self.window.dragging)
        self.assertFalse(self.window.held)
        self.assertEqual(self.window._pet_count, before)

    def test_hit_test_alpha_scan_is_cached(self) -> None:
        w = self.window
        w._extents_cache.clear()
        with patch.object(
            SpritePetWindow, "_alpha_extents", wraps=SpritePetWindow._alpha_extents
        ) as scan:
            first = w._pose_extents("idle")
            second = w._pose_extents("idle")
        self.assertEqual(first, second)
        self.assertEqual(scan.call_count, 1)

    def test_static_frames_skip_repaint(self) -> None:
        w = self.window
        self._reset_resource_alert()
        saved = (w.action.until, w.message, w.message_until, w._last_render_sig)
        calls: list[int] = []
        try:
            w.action.until = 0.0
            w.message = ""
            w.message_until = 0.0
            w._last_render_sig = None
            with patch.object(w, "update", lambda: calls.append(1)):
                w._update_if_dirty()  # first paint of this frame
                w._update_if_dirty()  # nothing changed -> skipped
                self.assertEqual(len(calls), 1)
                w.message = "你好"
                w.message_until = time.time() + 5.0
                w._update_if_dirty()  # bubble appeared -> repaint
                self.assertEqual(len(calls), 2)
        finally:
            w.action.until, w.message, w.message_until, w._last_render_sig = saved

    def test_sustained_high_memory_forces_visible_reaction(self) -> None:
        self._reset_resource_alert()
        self.window._memory_samples = 0
        sample = self._telemetry(cpu=10, mem=97)
        for _ in range(5):
            self.window._on_telemetry(sample)
        self.assertEqual(self.window._resource_alert_kind, "memory")
        self.assertGreater(self.window._resource_alert_until, 0)
        self.assertEqual(self.window._resource_alert_text, "唔…有点挤呢。")

    def test_sustained_high_cpu_forces_visible_reaction(self) -> None:
        self._reset_resource_alert()
        self.window._load = 0.0
        self.window._busy_samples = 0
        sample = self._telemetry(cpu=90, mem=40)
        for _ in range(4):
            self.window._on_telemetry(sample)
        self.assertEqual(self.window._resource_alert_kind, "load")
        self.assertEqual(self.window._current_sprite_name(), "surprised")
        self.assertEqual(self.window._resource_alert_text, "呼…忙得有点喘了。")

    def test_model_vram_usage_overrides_sleeping_pose(self) -> None:
        self._reset_resource_alert()
        self.window._vram_samples = 0
        self.window.mood.sleepiness = 1.0
        sample = self._telemetry(cpu=8, mem=40, gpu=60, vram=72, gpu_sampled=True)
        for _ in range(2):
            self.window._on_telemetry(sample)
        self.assertEqual(self.window._resource_alert_kind, "vram")
        self.assertEqual(self.window._current_sprite_name(), "surprised")
        self.assertEqual(self.window._resource_alert_text, "呼…有点忙不过来了。")

    def test_resource_state_does_not_repeat_or_keep_stale_numbers(self) -> None:
        self._reset_resource_alert()
        self.window._vram_samples = 0
        sample = self._telemetry(cpu=8, mem=40, gpu=60, vram=72, gpu_sampled=True)
        for _ in range(2):
            self.window._on_telemetry(sample)
        first_until = self.window._resource_alert_until
        first_message_until = self.window.message_until
        changed = self._telemetry(cpu=9, mem=41, gpu=65, vram=74, gpu_sampled=True)
        for _ in range(4):
            self.window._on_telemetry(changed)
        self.assertEqual(self.window._resource_alert_until, first_until)
        self.assertEqual(self.window.message_until, first_message_until)
        self.assertNotIn("72", self.window.message)
        self.assertEqual(self.window._gpu, 65)
        self.assertEqual(self.window._gpu_memory, 74)
        self.assertNotIn("GPU", self.window.tray.toolTip())
        self.assertNotIn("显存", self.window.tray.toolTip())

    def test_resource_state_recovers_after_three_cool_samples(self) -> None:
        self._reset_resource_alert()
        self.window._vram_samples = 2
        self.window._enter_resource_state("vram", "surprised", "嘿，这有点过火了！")
        cool = self._telemetry(cpu=5, mem=40, gpu=8, vram=70, gpu_sampled=True)
        for _ in range(3):
            self.window._on_telemetry(cool)
        self.assertFalse(self.window._resource_busy)
        self.assertEqual(self.window._resource_alert_kind, "")
        self.assertEqual(self.window._resource_alert_until, 0.0)

    def test_resource_state_escalates_to_more_important_pressure(self) -> None:
        self._reset_resource_alert()
        self.window._memory_samples = 5
        self.window._on_telemetry(self._telemetry(cpu=20, mem=97))
        self.assertEqual(self.window._resource_alert_kind, "memory")

        self.window._vram_samples = 1
        for _ in range(2):
            self.window._on_telemetry(
                self._telemetry(cpu=20, mem=97, gpu=70, vram=72, gpu_sampled=True)
            )
        self.assertEqual(self.window._resource_alert_kind, "vram")
        self.assertEqual(self.window._resource_alert_pose, "surprised")

    def test_vram_resource_state_recovers_if_gpu_sampling_disappears(self) -> None:
        self._reset_resource_alert()
        self.window._enter_resource_state("vram", "surprised", "嘿，这有点过火了！")
        stale = self._telemetry(cpu=8, mem=40, gpu=88, vram=72, gpu_sampled=False)
        for _ in range(8):
            self.window._on_telemetry(stale)
        self.assertFalse(self.window._resource_busy)
        self.assertEqual(self.window._resource_alert_kind, "")

    def test_night_only_beats_never_fire_in_daytime(self) -> None:
        w = self.window
        saved = (w._hour, w.mood.sleepiness, w.settings.activity)
        try:
            w.settings.activity = "low"  # keep it put so it samples poses, not walks
            # daytime, drowsy from being ignored: it may doze upright but must not
            # ride the moon or fully bed down for the night.
            w._hour = 14
            w.mood.sleepiness = 0.9
            day_poses = set()
            for _ in range(300):
                w.action.until = 0.0  # clear the lock so each call re-samples
                w._begin_idle_beat(0.0)
                day_poses.add(w.action.name)
            self.assertNotIn("moon", day_poses)
            self.assertNotIn("sleep", day_poses)

            # night, same drowsiness: the night-flavored beats are back in play,
            # proving the gate disables them by time, not outright.
            w._hour = 2
            night_poses = set()
            for _ in range(300):
                w.action.until = 0.0  # clear the lock so each call re-samples
                w._begin_idle_beat(0.0)
                night_poses.add(w.action.name)
            self.assertTrue({"moon", "sleep"} & night_poses)
        finally:
            w._hour, w.mood.sleepiness, w.settings.activity = saved

    def test_peeks_over_the_side_when_settling_at_a_screen_edge(self) -> None:
        w = self.window
        saved_was_parked = w._was_parked
        try:
            min_x, max_x = w._x_bounds()
            # right at the edge -> peeks over; in the middle -> not "parked" at all
            w.move(max_x, w.y())
            self.assertTrue(w._at_screen_edge())
            w.falling = False
            w._was_parked = False
            w.action.until = 0.0
            w._check_park_transition()
            self.assertEqual(w.action.name, "peek")

            mid = (min_x + max_x) // 2
            w.move(mid, w.y())
            self.assertFalse(w._at_screen_edge())
        finally:
            w._was_parked = saved_was_parked
            w._snap_to_taskbar(initial=True)

    def test_idle_beat_does_not_chase_cursor_horizontally(self) -> None:
        w = self.window
        saved = (
            w.settings.activity,
            w.mood.energy,
            w.mood.mood,
            w.mood.curiosity,
            w.mood.sleepiness,
            w.mood.attention,
            w.walking,
            w.walk_target_x,
        )
        old_start_walk_toward = w._start_walk_toward
        try:
            w.settings.activity = "high"
            w.mood.energy = 0.0
            w.mood.mood = 0.6
            w.mood.curiosity = 1.0
            w.mood.sleepiness = 0.0
            w.mood.attention = 1.0
            w.walking = False
            w.walk_target_x = None

            def fail_if_called(cursor_x: int) -> bool:
                raise AssertionError("idle beats must not target the cursor for walking")

            w._start_walk_toward = fail_if_called  # type: ignore[method-assign]
            with patch("pet.pet_window.random.random", return_value=0.0):
                w._begin_idle_beat(0.0)
        finally:
            (
                w.settings.activity,
                w.mood.energy,
                w.mood.mood,
                w.mood.curiosity,
                w.mood.sleepiness,
                w.mood.attention,
                w.walking,
                w.walk_target_x,
            ) = saved
            w._start_walk_toward = old_start_walk_toward  # type: ignore[method-assign]

    def test_high_energy_long_stroll_can_become_a_dash(self) -> None:
        w = self.window
        saved_energy = w.mood.energy
        try:
            w._snap_to_taskbar(initial=True)
            min_x, max_x = w._wander_x_bounds()
            w.move(min_x, w.y())
            w.mood.energy = 1.0
            with patch("pet.pet_window.random.randint", return_value=max_x), \
                 patch("pet.pet_window.random.random", return_value=0.0):
                self.assertTrue(w._start_walk())
            self.assertTrue(w._dashing)
            w._end_walk()

            w.move(min_x, w.y())
            w.mood.energy = 0.2  # calm strolls never dash
            with patch("pet.pet_window.random.randint", return_value=max_x), \
                 patch("pet.pet_window.random.random", return_value=0.0):
                self.assertTrue(w._start_walk())
            self.assertFalse(w._dashing)
            w._end_walk()
        finally:
            w.mood.energy = saved_energy
            w._snap_to_taskbar(initial=True)

    def test_teleport_blinks_across_the_taskbar(self) -> None:
        w = self.window
        try:
            w._snap_to_taskbar(initial=True)
            min_x, max_x = w._wander_x_bounds()
            w.move(min_x, w.y())
            with patch("pet.pet_window.random.randint", return_value=max_x), \
                 patch("pet.pet_window.random.random", return_value=0.9):
                self.assertTrue(w._start_teleport())
            self.assertEqual(w.action.name, "teleport")
            self.assertEqual(w._teleport_target, max_x)
            w._finish_teleport()
            self.assertEqual(w.x(), max_x)
            self.assertIsNone(w._teleport_target)

            # a hop too short to be worth the spell is refused
            w.move(max_x, w.y())
            with patch("pet.pet_window.random.randint", return_value=max_x - 60):
                self.assertFalse(w._start_teleport())
        finally:
            w.action.until = 0.0
            w._snap_to_taskbar(initial=True)

    def test_teleport_aborts_if_grabbed_mid_spell(self) -> None:
        w = self.window
        try:
            w._snap_to_taskbar(initial=True)
            start_x = w.x()
            w._teleport_target = start_x + 500
            w.held = True
            w._finish_teleport()
            self.assertEqual(w.x(), start_x)
            self.assertIsNone(w._teleport_target)
        finally:
            w.held = False
            w.action.until = 0.0
            w._snap_to_taskbar(initial=True)

    def test_fast_cursor_rush_startles_it_into_hiding(self) -> None:
        w = self.window
        saved = (w._cursor_was_near, w._last_glance, w._cursor_speed,
                 w.mood.sleepiness, w.action.until)
        try:
            w.action.until = 0.0
            w._cursor_was_near = False
            w._last_glance = 0.0
            w._cursor_speed = 2000.0
            w.mood.sleepiness = 0.0
            center = w.geometry().center()
            with patch("pet.pet_window.QCursor") as cursor_cls, \
                 patch("pet.pet_window.random.random", return_value=0.0):
                cursor_cls.pos.return_value = center
                w._maybe_notice_cursor(time.time())
            self.assertEqual(w.action.name, "hide")
        finally:
            (w._cursor_was_near, w._last_glance, w._cursor_speed,
             w.mood.sleepiness, w.action.until) = saved

    def test_first_pat_of_the_day_offers_a_gift(self) -> None:
        w = self.window
        saved = (w._gift_day, w._pet_count, w._last_pet_t, w.action.until)
        try:
            w._gift_day = -1
            w._pet_count = 0
            w._last_pet_t = 0.0
            w._on_pet()
            self.assertEqual(w.action.name, "gift")
            self.assertEqual(w._gift_day, time.localtime().tm_yday)

            w._on_pet()  # second pat, same day -> back to the normal pat ladder
            self.assertNotEqual(w.action.name, "gift")
        finally:
            (w._gift_day, w._pet_count, w._last_pet_t, w.action.until) = saved

    def test_autonomous_walk_avoids_user_parking_zones(self) -> None:
        w = self.window
        min_x, max_x = w._x_bounds()
        walk_min, walk_max = w._wander_x_bounds()
        self.assertEqual(walk_min, min_x + w.PARK_ZONE + 1)
        self.assertEqual(walk_max, max_x - w.PARK_ZONE - 1)

        w.move((min_x + max_x) // 2, w.y())
        self.assertTrue(w._start_walk_toward(max_x + w.width()))
        self.assertEqual(w.walk_target_x, walk_max)
        w.move(w.walk_target_x, w.y())
        self.assertFalse(w._is_parked())
        w._end_walk()

    def test_edge_extents_use_idle_body_not_effects(self) -> None:
        idle = self.window._alpha_extents(self.window.sprite_images["idle"])
        self.assertIsNotNone(idle)
        left, top, right, _bottom = idle
        self.assertEqual(self.window._content_left, left)
        self.assertEqual(self.window._content_top, top)
        self.assertEqual(self.window._content_right, self.window.SPRITE_SIZE - 1 - right)

    def test_side_glance_faces_cursor(self) -> None:
        self.assertIn("look_side_flip", self.window.sprite_images)
        self.assertEqual(self.window._side_glance_pose(10, 20), "look_side_flip")
        self.assertEqual(self.window._side_glance_pose(30, 20), "look_side")

    def test_tray_menu_does_not_expose_monitoring_ui(self) -> None:
        labels = [
            action.text()
            for action in self.window.tray.contextMenu().actions()
            if not action.isSeparator()
        ]
        joined = " ".join(labels)
        for technical_term in ("实时状态", "CPU", "GPU", "显存", "内存"):
            self.assertNotIn(technical_term, joined)

    def test_normal_resource_fluctuations_do_not_trigger(self) -> None:
        self._reset_resource_alert()
        self.window._busy_samples = 0
        self.window._memory_samples = 0
        self.window._vram_samples = 0

        # One real GPU peak followed by cached copies is still only one sample.
        self.window._on_telemetry(
            self._telemetry(cpu=12, mem=91, gpu=70, vram=72, gpu_sampled=True)
        )
        for _ in range(5):
            self.window._on_telemetry(
                self._telemetry(cpu=18, mem=91, gpu=70, vram=72, gpu_sampled=False)
            )

        self.assertFalse(self.window._resource_busy)
        self.assertEqual(self.window._vram_samples, 1)
        self.assertEqual(self.window._memory_samples, 0)

    @staticmethod
    def _telemetry(
        *,
        cpu: float,
        mem: float,
        gpu: float | None = None,
        vram: float | None = None,
        gpu_sampled: bool = False,
    ) -> Telemetry:
        return Telemetry(
            cpu=cpu,
            mem=mem,
            gpu=gpu,
            gpu_memory_pct=vram,
            gpu_sampled=gpu_sampled,
            battery_pct=None,
            plugged=None,
            hour=12,
            minute=0,
            idle_seconds=0,
        )

    def _reset_resource_alert(self) -> None:
        self.window._resource_alert_until = 0.0
        self.window._resource_alert_kind = ""
        self.window._resource_alert_priority = 0
        self.window._resource_alert_text = ""
        self.window._resource_busy = False
        self.window._resource_recovery_samples = 0
        self.window._resource_message_active = False
        self.window._vram_stale_samples = 0

    def _character_point(self) -> QPoint:
        dpr = self.window._current_dpr()
        stage_size = self.window._stage_logical_size(dpr)
        scale = stage_size / self.window.STAGE_SIZE
        stage_x = (self.window.width() - stage_size) / 2.0
        stage_y = (
            self.window.profile.ground_y
            - stage_size
            + self.window._foot_inset * scale
        )
        name, x_offset, y_offset = self.window._current_render_spec()
        image = self.window.sprite_images.get(name) or self.window.sprite_images["idle"]
        left, top, right, bottom = self.window._alpha_extents(image) or (0, 0, 0, 0)
        point = QPoint(
            round(stage_x + (self.window.SPRITE_X + x_offset + (left + right) / 2) * scale),
            round(stage_y + (self.window.SPRITE_Y + y_offset + (top + bottom) / 2) * scale),
        )
        return point

    @staticmethod
    def _mouse_event(
        event_type: QEvent.Type,
        local: QPoint,
        global_point: QPoint,
        button: Qt.MouseButton,
        buttons: Qt.MouseButton,
    ) -> QMouseEvent:
        return QMouseEvent(
            event_type,
            QPointF(local),
            QPointF(global_point),
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )


if __name__ == "__main__":
    unittest.main()
