from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTextBrowser,
)

from pet.monitor import Telemetry
from pet.moon_phase import PHASE_NAMES, MoonPhase
from pet.paths import DATA_DIR
from pet.pet_window import SpritePetWindow
from pet.settings import Settings
from pet.state import PetState
from pet.version import APP_VERSION


class _FakeMonitor(QObject):
    telemetry = Signal(object)

    def __init__(self, parent=None, *, active: bool = True) -> None:
        super().__init__(parent)
        self.active = active
        self.stopped = False

    def set_active(self, active: bool) -> None:
        self.active = active

    def shutdown(self) -> None:
        self.stopped = True


class WindowInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.tray_available_patcher = patch.object(
            QSystemTrayIcon,
            "isSystemTrayAvailable",
            return_value=True,
        )
        self.tray_available_patcher.start()
        self.state_dir = tempfile.TemporaryDirectory()
        state_path = Path(self.state_dir.name) / "state.json"
        with patch("pet.pet_window.SystemMonitor", _FakeMonitor):
            self.window = SpritePetWindow(
                Settings(),
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
        self.window.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.window._shutdown()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.state_dir.cleanup()
        self.tray_available_patcher.stop()

    def test_outer_transparent_corner_is_outside_character(self) -> None:
        self.assertFalse(self.window._is_interactive_point(QPoint(1, 1)))

    def test_character_body_is_grabbable(self) -> None:
        point = self._character_point()
        self.assertTrue(self.window._is_interactive_point(point))

    def test_native_input_mask_has_no_cursor_entry_race(self) -> None:
        self.window.message = ""
        self.window.message_until = 0.0
        self.window._input_mask_signature = None
        self.window._update_input_mask()

        mask = self.window.mask()
        self.assertTrue(mask.contains(self._character_point()))
        self.assertFalse(mask.contains(QPoint(1, 1)))

    def test_native_input_mask_includes_visible_speech_bubble(self) -> None:
        self.window.say("这里也可以点击", 5.0, force=True)
        self.window.repaint()
        self.app.processEvents()
        bubble = self.window._active_bubble_hit_rect()
        self.assertIsNotNone(bubble)
        assert bubble is not None
        self.window._input_mask_signature = None
        self.window._update_input_mask()
        self.assertTrue(self.window.mask().contains(bubble.center()))

    def test_character_attention_center_tracks_visible_pose(self) -> None:
        point = self.window.mapFromGlobal(self.window._character_center_global())
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

    def test_drag_can_handoff_to_another_monitor(self) -> None:
        w = self.window
        w._snap_to_taskbar(initial=True)
        point = self._character_point()
        global_start = w.mapToGlobal(point)
        global_end = QPoint(2250, 400)
        local_end = point + (global_end - global_start)

        class _Screen:
            @staticmethod
            def availableGeometry() -> QRect:
                return QRect(2000, 0, 1200, 900)

            @staticmethod
            def devicePixelRatio() -> float:
                return 1.5

        w.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress,
            point,
            global_start,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        ))
        try:
            with patch(
                "pet.pet_window.QApplication.screenAt",
                return_value=_Screen(),
            ):
                w.mouseMoveEvent(self._mouse_event(
                    QEvent.Type.MouseMove,
                    local_end,
                    global_end,
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.LeftButton,
                ))
            self.assertGreater(w.x(), 1800)
        finally:
            w.dragging = False
            w.held = False
            w._drag_history.clear()
            w._snap_to_taskbar(initial=True)

    def test_pause_before_release_does_not_reuse_old_throw_velocity(self) -> None:
        w = self.window
        w._snap_to_taskbar(initial=True)
        w.move(w.x(), w._rest_y() - 50)
        w.dragging = True
        w.drag_started = True
        w.held = True
        old = time.monotonic() - 0.3
        w._drag_history = [(old, 0, 0), (old + 0.05, 500, -500)]
        point = self._character_point()
        w.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease,
            point,
            w.mapToGlobal(point),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        ))
        try:
            self.assertTrue(w.falling)
            self.assertEqual(w.fall_vx, 0.0)
            self.assertEqual(w.fall_vy, 0.0)
        finally:
            w.falling = False
            w.phys_timer.stop()
            w._snap_to_taskbar(initial=True)

    def test_lost_mouse_capture_cannot_leave_pet_held(self) -> None:
        w = self.window
        w.dragging = True
        w.held = True
        w._drag_history = [(time.monotonic(), 1, 1)]
        QApplication.sendEvent(w, QEvent(QEvent.Type.UngrabMouse))
        self.assertFalse(w.dragging)
        self.assertFalse(w.held)
        self.assertEqual(w._drag_history, [])

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
                w.message_until = time.monotonic() + 5.0
                w._update_if_dirty()  # bubble appeared -> repaint
                self.assertEqual(len(calls), 2)
        finally:
            w.action.until, w.message, w.message_until, w._last_render_sig = saved

    def test_disabled_runtime_stops_animation_and_motion_timers(self) -> None:
        w = self.window
        try:
            w.falling = True
            w.phys_timer.start()
            w.walking = True
            w.walk_timer.start()
            w._moon_focus_timer.start()
            w._toggle_enabled(False)
            self.assertFalse(w.isVisible())
            self.assertFalse(w._moon_corner.isVisible())
            self.assertFalse(w.anim_timer.isActive())
            self.assertFalse(w.state_timer.isActive())
            self.assertFalse(w.phys_timer.isActive())
            self.assertFalse(w.walk_timer.isActive())
            self.assertFalse(w._moon_focus_timer.isActive())
            self.assertFalse(w.falling)
            self.assertFalse(w.monitor.active)
        finally:
            w._toggle_enabled(True)
        self.assertTrue(w.anim_timer.isActive())
        self.assertTrue(w.state_timer.isActive())
        self.assertTrue(w._moon_corner.isVisible())
        self.assertTrue(w.monitor.active)

    def test_corner_moon_reads_committed_gift_and_focus_state(self) -> None:
        w = self.window
        moon = w._moon_corner
        w._state.last_gift_date = ""
        w._state.moon_tokens = 0
        with patch.object(moon, "pulse", wraps=moon.pulse) as pulse:
            gift = w._claim_daily_gift()
            self.assertIsNotNone(gift)
            self.assertEqual(moon.snapshot.moon_tokens, 1)
            pulse.assert_called_with("gift")

            w._start_focus(25)
            self.assertTrue(moon.snapshot.focus_active)
            self.assertTrue(w._moon_focus_timer.isActive())
            pulse.assert_called_with("focus")

            w._complete_focus()
            self.assertFalse(moon.snapshot.focus_active)
            self.assertEqual(moon.snapshot.focus_minutes_completed, 25)
            pulse.assert_called_with("focus_complete")

    def test_failed_daily_gift_never_pulses_or_grows_corner_moon(self) -> None:
        w = self.window
        moon = w._moon_corner
        w._state.last_gift_date = ""
        w._state.moon_tokens = 0
        w._sync_moon_corner()
        with (
            patch.object(w, "_save_state", return_value=False),
            patch.object(moon, "pulse") as pulse,
        ):
            self.assertIsNone(w._claim_daily_gift())
        self.assertEqual(w._state.moon_tokens, 0)
        self.assertEqual(moon.snapshot.moon_tokens, 0)
        pulse.assert_not_called()

    def test_failed_focus_save_does_not_grow_or_pulse_corner_moon(self) -> None:
        w = self.window
        moon = w._moon_corner
        w._state.focus_sessions_completed = 2
        w._state.focus_minutes_completed = 50
        w._sync_moon_corner()
        w._start_focus(25)
        with (
            patch.object(w, "_save_state", return_value=False),
            patch.object(moon, "pulse") as pulse,
        ):
            w._complete_focus()
        self.assertEqual(w._state.focus_sessions_completed, 2)
        self.assertEqual(w._state.focus_minutes_completed, 50)
        self.assertEqual(moon.snapshot.focus_minutes_completed, 50)
        pulse.assert_not_called()

    def test_walk_position_is_not_advanced_by_the_slow_sprite_clock(self) -> None:
        w = self.window
        w.walking = True
        w.walk_target_x = w.x() + 200
        w._walk_pos_f = float(w.x())
        with patch.object(w, "_step_walk") as step_walk:
            w._on_anim()
        step_walk.assert_not_called()

    def test_display_change_events_share_one_debounce_timer(self) -> None:
        w = self.window
        w._display_timer.stop()
        w._on_display_changed()
        timer_id = w._display_timer.timerId()
        w._on_display_changed()
        self.assertTrue(w._display_timer.isSingleShot())
        self.assertTrue(w._display_timer.isActive())
        self.assertNotEqual(timer_id, -1)

    def test_clock_rollback_cannot_reverse_mood_drift(self) -> None:
        w = self.window
        saved = (
            w._last_brain_t,
            w._active_streak,
            w.mood.energy,
            w.mood.mood,
            w.mood.curiosity,
            w.mood.sleepiness,
            w.mood.attention,
        )
        try:
            w._last_brain_t = 100.0
            before = (
                w._active_streak,
                w.mood.energy,
                w.mood.mood,
                w.mood.curiosity,
                w.mood.sleepiness,
                w.mood.attention,
            )
            w._update_brain(90.0)
            after = (
                w._active_streak,
                w.mood.energy,
                w.mood.mood,
                w.mood.curiosity,
                w.mood.sleepiness,
                w.mood.attention,
            )
            self.assertEqual(after, before)
        finally:
            (
                w._last_brain_t,
                w._active_streak,
                w.mood.energy,
                w.mood.mood,
                w.mood.curiosity,
                w.mood.sleepiness,
                w.mood.attention,
            ) = saved

    def test_first_reaction_is_not_suppressed_soon_after_boot(self) -> None:
        w = self.window
        saved_action = (w.action.name, w.action.until, w.action.locked)
        saved = (saved_action, dict(w._react_last), w.held, w.dragging, w.falling)
        try:
            w.action.until = 0.0
            w._react_last.pop("fresh-start", None)
            w.held = w.dragging = w.falling = False
            with patch("pet.pet_window.time.monotonic", return_value=1.0):
                self.assertTrue(
                    w._react("wave", 1.0, "fresh-start", cooldown=300.0)
                )
        finally:
            action, reactions, w.held, w.dragging, w.falling = saved
            w.action = type(w.action)(*action)
            w._react_last = reactions

    def test_window_close_hides_pet_but_keeps_tray_runtime_alive(self) -> None:
        w = self.window
        try:
            w.close()
            self.app.processEvents()
            self.assertTrue(w.settings.enabled)
            self.assertTrue(w.act_enabled.isChecked())
            self.assertFalse(w._runtime_active)
            self.assertFalse(w.isVisible())
            self.assertFalse(w.monitor.stopped)
            self.assertFalse(w.monitor.active)
        finally:
            w._recall_pet()

    def test_no_tray_close_exits_instead_of_creating_a_ghost_process(self) -> None:
        w = self.window
        event = MagicMock()
        # Exercise the real failure mode: Explorer disappeared after the last
        # cached "available" status.
        w._tray_available = True
        with (
            patch.object(
                QSystemTrayIcon,
                "isSystemTrayAvailable",
                return_value=False,
            ),
            patch.object(QApplication, "quit") as quit_app,
        ):
            w.closeEvent(event)
        event.accept.assert_called_once()
        quit_app.assert_called_once()
        self.assertTrue(w._shutdown_done)
        self.assertTrue(w.monitor.stopped)

    def test_hidden_pet_recalls_itself_if_the_tray_disappears(self) -> None:
        w = self.window
        w._set_runtime_active(False)
        self.assertTrue(w._tray_watchdog.isActive())
        with patch.object(
            QSystemTrayIcon,
            "isSystemTrayAvailable",
            return_value=False,
        ):
            w._check_hidden_tray()
            self.assertFalse(w._runtime_active)
            w._check_hidden_tray()
        self.assertTrue(w._runtime_active)
        self.assertTrue(w.isVisible())
        self.assertFalse(w._tray_watchdog.isActive())

    def test_disabled_startup_preserves_choice_and_exits_without_a_tray(self) -> None:
        state_path = Path(self.state_dir.name) / "no-tray-state.json"
        settings = Settings(enabled=False)
        settings.save = MagicMock()  # type: ignore[method-assign]
        with (
            patch("pet.pet_window.SystemMonitor", _FakeMonitor),
            patch.object(QTimer, "singleShot") as single_shot,
            patch.object(
                QSystemTrayIcon,
                "isSystemTrayAvailable",
                return_value=False,
            ),
        ):
            window = SpritePetWindow(
                settings,
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
        try:
            self.assertFalse(window.settings.enabled)
            self.assertFalse(window._runtime_active)
            self.assertFalse(window.isVisible())
            self.assertTrue(window._exit_for_missing_tray)
            self.assertFalse(window.act_visibility.isEnabled())
            single_shot.assert_called_once()
            callback = single_shot.call_args.args[1]
            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.No,
                ) as question,
                patch.object(QApplication, "quit") as quit_app,
            ):
                callback()
            question.assert_called_once()
            quit_app.assert_called_once()
            self.assertTrue(window._shutdown_done)
        finally:
            window._shutdown()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_disabled_startup_can_recover_without_a_system_tray(self) -> None:
        state_path = Path(self.state_dir.name) / "no-tray-recover-state.json"
        settings = Settings(enabled=False)
        settings.save = MagicMock()  # type: ignore[method-assign]
        with (
            patch("pet.pet_window.SystemMonitor", _FakeMonitor),
            patch.object(QTimer, "singleShot") as single_shot,
            patch.object(
                QSystemTrayIcon,
                "isSystemTrayAvailable",
                return_value=False,
            ),
        ):
            window = SpritePetWindow(
                settings,
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
            callback = single_shot.call_args.args[1]
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                callback()
        try:
            self.assertTrue(window.settings.enabled)
            self.assertTrue(window._runtime_active)
            self.assertTrue(window.isVisible())
            self.assertFalse(window._exit_for_missing_tray)
            self.assertIn("我回来啦", window.message)
            settings.save.assert_called()
        finally:
            window._shutdown()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_disabled_startup_tray_notification_is_actionable(self) -> None:
        state_path = Path(self.state_dir.name) / "disabled-tray-state.json"
        settings = Settings(enabled=False)
        settings.save = MagicMock()  # type: ignore[method-assign]
        with (
            patch("pet.pet_window.SystemMonitor", _FakeMonitor),
            patch.object(QTimer, "singleShot") as single_shot,
        ):
            window = SpritePetWindow(
                settings,
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
        try:
            callback = single_shot.call_args.args[1]
            with patch.object(window.tray, "showMessage") as show_message:
                callback()
            show_message.assert_called_once()
            self.assertIn("单击", show_message.call_args.args[1])

            window._on_tray_message_clicked()
            self.assertTrue(window._runtime_active)
            self.assertTrue(window.settings.enabled)
        finally:
            window._shutdown()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_explicitly_disabled_pet_exits_if_its_tray_entry_disappears(self) -> None:
        w = self.window
        w._toggle_enabled(False)
        self.assertFalse(w.settings.enabled)
        with (
            patch.object(
                QSystemTrayIcon,
                "isSystemTrayAvailable",
                return_value=False,
            ),
            patch.object(QApplication, "quit") as quit_app,
        ):
            w._check_hidden_tray()
            w._check_hidden_tray()
        self.assertFalse(w.settings.enabled)
        self.assertFalse(w._runtime_active)
        self.assertTrue(w._shutdown_done)
        quit_app.assert_called_once()

    def test_tray_click_recalls_but_never_accidentally_hides(self) -> None:
        w = self.window
        self.assertTrue(w._runtime_active)
        w._tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertTrue(w._runtime_active)
        self.assertTrue(w.settings.enabled)
        self.assertTrue(w.isVisible())

    def test_recall_recovers_every_transient_state(self) -> None:
        w = self.window
        w._toggle_enabled(False)
        w.collapsed = True
        w.dragging = True
        w.held = True
        w.falling = True
        w.walking = True
        w.walk_target_x = w.x() + 100
        w._teleport_target = w.x() + 200
        w._recall_pet()
        self.assertTrue(w.settings.enabled)
        self.assertTrue(w._runtime_active)
        self.assertTrue(w.isVisible())
        self.assertFalse(w.collapsed)
        self.assertFalse(w.dragging)
        self.assertFalse(w.held)
        self.assertFalse(w.falling)
        self.assertFalse(w.walking)
        self.assertIsNone(w.walk_target_x)
        self.assertIsNone(w._teleport_target)
        self.assertTrue(w.anim_timer.isActive())
        self.assertTrue(w.monitor.active)

    def test_first_launch_greeting_teaches_core_controls(self) -> None:
        self.assertLess(self.window._startup_absence, 0)
        self.assertIn("拖", self.window.message)
        self.assertIn("托盘", self.window.message)
        self.assertTrue(self.window._state.first_seen_date)

    def test_focus_mode_is_quiet_persisted_and_reversible(self) -> None:
        w = self.window
        try:
            w._start_focus(25)
            self.assertTrue(w._focus_active)
            self.assertTrue(w._quiet)
            self.assertFalse(w._lively)
            self.assertEqual(w.action.name, "read")
            self.assertIn("25", w.message)
            self.assertGreater(w._state.focus_until, time.time())
            self.assertTrue(w._focus_timer.isActive())
            self.assertTrue(w._focus_status_timer.isActive())
            self.assertTrue(w.act_focus_end.isEnabled())
            self.assertFalse(w.act_focus_25.isEnabled())
            self.assertFalse(w.act_focus_50.isEnabled())
            self.assertFalse(w.act_focus_90.isEnabled())
            self.assertIn("专注中", w.status_action.text())
            self.assertIn("还剩", w.act_focus_end.text())

            w.action.until = 0.0
            w._begin_idle_beat(time.monotonic())
            self.assertIn(w.action.name, {"read", "write", "sit", "blink"})
            self.assertGreaterEqual(w._next_idle_gap, 20.0)
        finally:
            w._cancel_focus()
        self.assertFalse(w._focus_active)
        self.assertFalse(w._quiet)
        self.assertEqual(w._state.focus_until, 0.0)
        self.assertFalse(w._focus_timer.isActive())

    def test_focus_start_persists_the_planned_minutes(self) -> None:
        w = self.window
        with patch.object(w, "_save_state", return_value=True) as save_state:
            w._start_focus(50)
            self.assertEqual(w._state.focus_planned_minutes, 50)
            self.assertGreater(w._state.focus_until, time.time())
            save_state.assert_called_once()
        w._cancel_focus()

    def test_focus_completion_updates_journal_totals_exactly_once(self) -> None:
        w = self.window
        today_key = date.today().isoformat()
        w._state.focus_sessions_completed = 2
        w._state.focus_minutes_completed = 75
        w._state.focus_today_date = today_key
        w._state.focus_today_minutes = 25

        w._start_focus(50)
        w._complete_focus()

        self.assertEqual(w._state.focus_planned_minutes, 0)
        self.assertEqual(w._state.focus_until, 0.0)
        self.assertEqual(w._focus_deadline, 0.0)
        self.assertEqual(w._state.focus_sessions_completed, 3)
        self.assertEqual(w._state.focus_minutes_completed, 125)
        self.assertEqual(w._state.focus_today_date, today_key)
        self.assertEqual(w._state.focus_today_minutes, 75)

        completed_snapshot = (
            w._state.focus_sessions_completed,
            w._state.focus_minutes_completed,
            w._state.focus_today_minutes,
        )
        w._complete_focus()
        self.assertEqual(
            (
                w._state.focus_sessions_completed,
                w._state.focus_minutes_completed,
                w._state.focus_today_minutes,
            ),
            completed_snapshot,
        )

    def test_focus_cancel_clears_plan_without_counting_a_completion(self) -> None:
        w = self.window
        today_key = date.today().isoformat()
        w._state.focus_sessions_completed = 4
        w._state.focus_minutes_completed = 160
        w._state.focus_today_date = today_key
        w._state.focus_today_minutes = 20

        w._start_focus(25)
        w._cancel_focus()

        self.assertEqual(w._state.focus_planned_minutes, 0)
        self.assertEqual(w._state.focus_until, 0.0)
        self.assertEqual(w._state.focus_sessions_completed, 4)
        self.assertEqual(w._state.focus_minutes_completed, 160)
        self.assertEqual(w._state.focus_today_minutes, 20)

    def test_focus_completion_is_single_and_visible(self) -> None:
        w = self.window
        w._start_focus(25)
        w._complete_focus()
        first_message = w.message
        self.assertFalse(w._focus_active)
        self.assertEqual(w._state.focus_until, 0.0)
        self.assertEqual(w.action.name, "star")
        self.assertIn("完成", first_message)
        self.assertIn("刚完成", w.status_action.text())
        self.assertFalse(w.act_focus_end.isEnabled())
        self.assertTrue(w.act_focus_25.isEnabled())
        w._complete_focus()
        self.assertEqual(w.message, first_message)

    def test_focus_notification_click_acknowledges_and_raises_pet(self) -> None:
        w = self.window
        w._start_focus(25)
        w._complete_focus()
        self.assertTrue(w._focus_completed_pending)
        with (
            patch.object(w, "raise_") as raise_window,
            patch.object(w, "activateWindow") as activate_window,
        ):
            w._on_tray_message_clicked()
        self.assertFalse(w._focus_completed_pending)
        self.assertEqual(w.action.name, "star")
        raise_window.assert_called_once()
        activate_window.assert_called_once()
        self.assertNotIn("刚完成", w.status_action.text())

    def test_focus_suppresses_ambient_interruptions(self) -> None:
        w = self.window
        w.settings.clipboard_reactions = True
        w._start_focus(25)
        for _ in range(6):
            w._on_telemetry(self._telemetry(cpu=99, mem=99))
        self.assertFalse(w._resource_busy)

        clipboard = MagicMock()
        clipboard.mimeData.return_value.hasText.return_value = True
        before_attention = w.mood.attention
        with patch("pet.pet_window.QApplication.clipboard", return_value=clipboard):
            w._on_clipboard()
        self.assertEqual(w.mood.attention, before_attention)
        self.assertFalse(
            w._react("wave", 1.0, "ambient", cooldown=0.0)
        )
        w._cancel_focus()

    def test_active_focus_restores_from_persisted_state(self) -> None:
        state_path = Path(self.state_dir.name) / "focus-state.json"
        PetState(
            first_seen_date=time.strftime("%Y-%m-%d"),
            focus_until=time.time() + 600,
        ).save(state_path)
        with patch("pet.pet_window.SystemMonitor", _FakeMonitor):
            restored = SpritePetWindow(
                Settings(),
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
        restored.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
        try:
            self.assertTrue(restored._focus_active)
            self.assertTrue(restored._focus_timer.isActive())
            self.assertEqual(restored.action.name, "read")
            self.assertIn("专注中", restored.status_action.text())
            self.assertIn("还剩约", restored.message)
            self.assertIn("分钟", restored.message)
        finally:
            restored._shutdown()
            restored.close()
            restored.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_legacy_active_focus_infers_plan_from_remaining_minutes(self) -> None:
        state_path = Path(self.state_dir.name) / "legacy-focus-state.json"
        wall_now = time.time()
        monotonic_now = time.monotonic()
        state_path.write_text(
            (
                '{"first_seen_date":"'
                + date.today().isoformat()
                + '","focus_until":'
                + str(wall_now + 601)
                + "}"
            ),
            encoding="utf-8",
        )

        with (
            patch("pet.pet_window.time.time", return_value=wall_now),
            patch("pet.pet_window.time.monotonic", return_value=monotonic_now),
            patch("pet.pet_window.SystemMonitor", _FakeMonitor),
        ):
            restored = SpritePetWindow(
                Settings(),
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
            restored.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
            try:
                self.assertTrue(restored._focus_active)
                self.assertEqual(restored._state.focus_planned_minutes, 11)
            finally:
                restored._shutdown()
                restored.close()
                restored.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_absurd_persisted_focus_is_cleared(self) -> None:
        state_path = Path(self.state_dir.name) / "bad-focus-state.json"
        PetState(
            first_seen_date=time.strftime("%Y-%m-%d"),
            focus_until=time.time() + SpritePetWindow.MAX_FOCUS_SECONDS + 60,
        ).save(state_path)
        with patch("pet.pet_window.SystemMonitor", _FakeMonitor):
            restored = SpritePetWindow(
                Settings(),
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
        restored.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
        try:
            self.assertFalse(restored._focus_active)
            self.assertEqual(restored._state.focus_until, 0.0)
            self.assertFalse(restored._focus_timer.isActive())
        finally:
            restored._shutdown()
            restored.close()
            restored.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_future_first_seen_date_is_repaired_on_startup(self) -> None:
        state_path = Path(self.state_dir.name) / "future-first-seen.json"
        PetState(first_seen_date="2999-12-31").save(state_path)
        with patch("pet.pet_window.SystemMonitor", _FakeMonitor):
            restored = SpritePetWindow(
                Settings(),
                Path(__file__).resolve().parents[1],
                state_path=state_path,
            )
        restored.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
        try:
            today_key = time.strftime("%Y-%m-%d")
            self.assertEqual(restored._state.first_seen_date, today_key)
            self.assertEqual(PetState.load(state_path).first_seen_date, today_key)
            self.assertEqual(restored._state.companionship_days(), 1)
        finally:
            restored._shutdown()
            restored.close()
            restored.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_clipboard_reactions_are_opt_in_and_stop_while_hidden(self) -> None:
        w = self.window
        clipboard = MagicMock()
        clipboard.mimeData.return_value.hasText.return_value = True
        before = w.mood.attention
        with patch("pet.pet_window.QApplication.clipboard", return_value=clipboard):
            w._on_clipboard()
        self.assertEqual(w.mood.attention, before)

        w._toggle_clipboard_reactions(True)
        w._last_clip_react = 0.0
        before = w.mood.attention
        with patch("pet.pet_window.QApplication.clipboard", return_value=clipboard):
            w._on_clipboard()
        self.assertGreater(w.mood.attention, before)

        w._set_runtime_active(False)
        hidden_attention = w.mood.attention
        w._last_clip_react = 0.0
        with patch("pet.pet_window.QApplication.clipboard", return_value=clipboard):
            w._on_clipboard()
        self.assertEqual(w.mood.attention, hidden_attention)
        w._recall_pet()

    def test_system_awareness_toggle_controls_sampling(self) -> None:
        w = self.window
        w._load = 0.8
        w._toggle_system_awareness(False)
        self.assertFalse(w.settings.system_awareness)
        self.assertFalse(w.monitor.active)
        self.assertEqual(w._load, 0.0)
        w._toggle_system_awareness(True)
        self.assertTrue(w.settings.system_awareness)
        self.assertTrue(w.monitor.active)

    def test_privacy_toggle_warns_when_setting_cannot_be_saved(self) -> None:
        w = self.window
        w.settings.save = MagicMock(  # type: ignore[method-assign]
            side_effect=PermissionError("read only")
        )
        try:
            w._toggle_system_awareness(False)
            self.assertFalse(w.settings.system_awareness)
            self.assertFalse(w.monitor.active)
            self.assertTrue(w._settings_save_failed)
            self.assertTrue(w._settings_dirty)
            self.assertIn("仅在本次运行中生效", w.message)
            self.assertIn("数据未保存", w.status_action.text())
        finally:
            w.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
            w._toggle_system_awareness(True)

    def test_disabling_warns_in_tray_when_preference_cannot_be_saved(self) -> None:
        w = self.window
        w.settings.save = MagicMock(  # type: ignore[method-assign]
            side_effect=PermissionError("read only")
        )
        try:
            with patch.object(w.tray, "showMessage") as show_message:
                w._toggle_enabled(False)
            self.assertFalse(w._runtime_active)
            self.assertTrue(w._settings_save_failed)
            self.assertIn("数据未保存", w.status_action.text())
            show_message.assert_called()
            self.assertIn("重启后可能恢复", show_message.call_args.args[1])
        finally:
            w.settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
            w._toggle_enabled(True)

    def test_queued_telemetry_is_dropped_after_awareness_is_disabled(self) -> None:
        w = self.window
        w._toggle_system_awareness(False)
        before = (w._telemetry_samples, w._load, w._cpu, w._mem)
        w._on_telemetry(self._telemetry(cpu=99, mem=99))
        self.assertEqual((w._telemetry_samples, w._load, w._cpu, w._mem), before)
        w._toggle_system_awareness(True)

    def test_companion_journal_shows_moon_and_gentle_local_memories(self) -> None:
        w = self.window
        today = date.today()
        w._state.first_seen_date = (today - timedelta(days=4)).isoformat()
        w._state.moon_tokens = 15
        w._state.focus_today_date = today.isoformat()
        w._state.focus_today_minutes = 50
        w._state.focus_sessions_completed = 3
        w._state.focus_minutes_completed = 100

        w._show_companion_journal()
        self.app.processEvents()

        dialogs = [
            dialog
            for dialog in w.findChildren(QDialog)
            if "陪伴手账" in dialog.windowTitle()
        ]
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        text = " ".join(
            [
                *(label.text() for label in dialog.findChildren(QLabel)),
                *(
                    browser.toPlainText()
                    for browser in dialog.findChildren(QTextBrowser)
                ),
            ]
        )
        normalized = " ".join(text.split())

        self.assertTrue(any(name in normalized for name in PHASE_NAMES))
        self.assertRegex(normalized, r"月龄.*天")
        self.assertRegex(normalized, r"亮面.*%")
        self.assertRegex(normalized, r"相识第\s*5\s*天")
        self.assertRegex(normalized, r"月光\s*15\s*枚")
        self.assertRegex(normalized, r"星晶\s*2\s*颗")
        self.assertRegex(
            normalized,
            r"(今日专注.*50\s*分钟|今天.*50\s*分钟.*专注)",
        )
        self.assertRegex(normalized, r"累计.*3\s*段.*100\s*分钟")
        self.assertRegex(normalized, r"不(计算|设|要求).*(连续签到|连续打卡)")
        self.assertRegex(normalized, r"不会.*(清零|扣)")
        self.assertRegex(normalized, r"不读取(位置|定位)")
        self.assertIn("不联网", normalized)
        for pressure_copy in ("漏签", "补签", "连续第", "必须打卡"):
            with self.subTest(pressure_copy=pressure_copy):
                self.assertNotIn(pressure_copy, normalized)
        dialog.close()

    def test_companion_journal_zero_today_uses_neutral_copy(self) -> None:
        w = self.window
        w._state.focus_today_date = date.today().isoformat()
        w._state.focus_today_minutes = 0

        w._show_companion_journal()
        self.app.processEvents()

        dialogs = [
            dialog
            for dialog in w.findChildren(QDialog)
            if "陪伴手账" in dialog.windowTitle()
        ]
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        text = " ".join(
            browser.toPlainText()
            for browser in dialog.findChildren(QTextBrowser)
        )
        normalized = " ".join(text.split())
        self.assertRegex(normalized, r"(今日专注|今天).*0\s*分钟")
        self.assertNotIn("还没有完成", normalized)
        self.assertNotIn("未完成", normalized)
        dialog.close()

    def test_tray_menu_has_keyboard_accessible_companion_journal_entry(
        self,
    ) -> None:
        actions = [
            action
            for action in self.window.tray.contextMenu().actions()
            if "陪伴手账" in action.text()
        ]

        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].isEnabled())
        actions[0].trigger()
        self.app.processEvents()

        dialogs = [
            dialog
            for dialog in self.window.findChildren(QDialog)
            if "陪伴手账" in dialog.windowTitle()
        ]
        self.assertEqual(len(dialogs), 1)
        dialogs[0].close()

    def test_daily_card_is_opaque_square_and_reflects_local_memories(
        self,
    ) -> None:
        w = self.window
        today = date.today()
        new_moon = MoonPhase(
            fraction=0.0,
            age_days=0.0,
            illumination=0.0,
            index=0,
            name="新月",
            emoji="🌑",
            event_key="0",
            event_distance_days=0.0,
        )
        full_moon = MoonPhase(
            fraction=0.5,
            age_days=14.8,
            illumination=1.0,
            index=4,
            name="满月",
            emoji="🌕",
            event_key="4",
            event_distance_days=0.0,
        )
        w._state.first_seen_date = today.isoformat()
        w._state.moon_tokens = 2
        w._state.focus_today_date = today.isoformat()
        w._state.focus_today_minutes = 0

        def build_digest(phase: MoonPhase) -> tuple[QImage, bytes]:
            with patch(
                "pet.pet_window.calculate_moon_phase",
                return_value=phase,
            ):
                image = w._build_daily_card()
            rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
            digest = hashlib.sha256(bytes(rgba.constBits())).digest()
            return image, digest

        baseline, baseline_digest = build_digest(new_moon)
        self.assertIsInstance(baseline, QImage)
        self.assertFalse(baseline.isNull())
        self.assertEqual((baseline.width(), baseline.height()), (1080, 1080))
        rgba = baseline.convertToFormat(QImage.Format.Format_RGBA8888)
        pixels = bytes(rgba.constBits())
        self.assertEqual(len(pixels), rgba.sizeInBytes())
        self.assertTrue(all(alpha == 255 for alpha in pixels[3::4]))

        _image, moon_digest = build_digest(full_moon)
        self.assertNotEqual(moon_digest, baseline_digest)

        w._state.first_seen_date = (today - timedelta(days=4)).isoformat()
        _image, days_digest = build_digest(new_moon)
        self.assertNotEqual(days_digest, baseline_digest)

        w._state.first_seen_date = today.isoformat()
        w._state.moon_tokens = 9
        _image, tokens_digest = build_digest(new_moon)
        self.assertNotEqual(tokens_digest, baseline_digest)

        w._state.moon_tokens = 2
        w._state.focus_today_minutes = 50
        _image, focus_digest = build_digest(new_moon)
        self.assertNotEqual(focus_digest, baseline_digest)

    def test_companion_journal_has_accessible_daily_card_button(self) -> None:
        w = self.window
        w._show_companion_journal()
        self.app.processEvents()

        dialogs = [
            dialog
            for dialog in w.findChildren(QDialog)
            if "陪伴手账" in dialog.windowTitle()
        ]
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        buttons = [
            button
            for button in dialog.findChildren(QPushButton)
            if button.text() == "保存今日卡片…"
        ]
        self.assertEqual(len(buttons), 1)
        button = buttons[0]
        self.assertTrue(button.isEnabled())
        self.assertIn("保存今日卡片", button.accessibleName())

        with patch(
            "pet.pet_window.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ) as choose_file:
            button.click()
        choose_file.assert_called_once()
        dialog.close()

    def test_save_daily_card_writes_png_and_cancel_writes_nothing(self) -> None:
        w = self.window
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / "MoonShell-today.png"
            with (
                patch(
                    "pet.pet_window.QFileDialog.getSaveFileName",
                    return_value=(str(target), "PNG 图片 (*.png)"),
                ),
                patch("pet.pet_window.QMessageBox.information"),
                patch("pet.pet_window.QMessageBox.warning") as warning,
            ):
                w._save_daily_card()

            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            loaded = QImage(str(target))
            self.assertFalse(loaded.isNull())
            self.assertEqual((loaded.width(), loaded.height()), (1080, 1080))
            warning.assert_not_called()

            before_cancel = {path.name for path in directory.iterdir()}
            with (
                patch(
                    "pet.pet_window.QFileDialog.getSaveFileName",
                    return_value=("", ""),
                ),
                patch("pet.pet_window.QMessageBox.information"),
                patch("pet.pet_window.QMessageBox.warning"),
            ):
                w._save_daily_card()
            self.assertEqual(
                {path.name for path in directory.iterdir()},
                before_cancel,
            )

    def test_non_png_card_selection_appends_png_without_overwriting_sibling(
        self,
    ) -> None:
        w = self.window
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            selected = directory / "same-name.jpg"
            existing_png = directory / "same-name.png"
            actual_target = directory / "same-name.jpg.png"
            existing_png.write_bytes(b"existing-png")

            with (
                patch(
                    "pet.pet_window.QFileDialog.getSaveFileName",
                    return_value=(str(selected), "PNG 图片 (*.png)"),
                ),
                patch("pet.pet_window.QMessageBox.question") as question,
                patch("pet.pet_window.QMessageBox.information") as information,
                patch("pet.pet_window.QMessageBox.warning") as warning,
            ):
                self.assertTrue(w._save_daily_card())

            question.assert_not_called()
            information.assert_called_once()
            warning.assert_not_called()
            self.assertEqual(existing_png.read_bytes(), b"existing-png")
            self.assertFalse(selected.exists())
            self.assertTrue(actual_target.is_file())
            self.assertEqual(
                actual_target.read_bytes()[:8],
                b"\x89PNG\r\n\x1a\n",
            )
            self.assertEqual(
                [
                    path.name
                    for path in directory.iterdir()
                    if path.name.endswith(".moonshell.tmp")
                ],
                [],
            )

    def test_existing_normalized_card_defaults_to_no_and_is_not_overwritten(
        self,
    ) -> None:
        w = self.window
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            selected = directory / "same-name.jpg"
            normalized_target = directory / "same-name.jpg.png"
            normalized_target.write_bytes(b"keep-this-card")

            with (
                patch(
                    "pet.pet_window.QFileDialog.getSaveFileName",
                    return_value=(str(selected), "PNG 图片 (*.png)"),
                ),
                patch(
                    "pet.pet_window.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.No,
                ) as question,
                patch.object(w, "_build_daily_card") as build_card,
                patch("pet.pet_window.os.replace") as replace,
                patch("pet.pet_window.QMessageBox.information") as information,
                patch("pet.pet_window.QMessageBox.warning") as warning,
            ):
                self.assertFalse(w._save_daily_card())

            question.assert_called_once()
            question_args = question.call_args.args
            self.assertIn(str(normalized_target), question_args[2])
            self.assertEqual(
                question_args[4],
                QMessageBox.StandardButton.No,
            )
            build_card.assert_not_called()
            replace.assert_not_called()
            information.assert_not_called()
            warning.assert_not_called()
            self.assertEqual(
                normalized_target.read_bytes(),
                b"keep-this-card",
            )
            self.assertFalse(selected.exists())
            self.assertEqual(
                [
                    path.name
                    for path in directory.iterdir()
                    if path.name.endswith(".moonshell.tmp")
                ],
                [],
            )

    def test_about_dialog_exposes_usage_privacy_version_and_data_location(self) -> None:
        self.window._show_about()
        dialog = self.window._about_dialog
        browser = dialog.findChild(QTextBrowser)
        self.assertIsNotNone(browser)
        assert browser is not None
        text = browser.toPlainText()
        labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn("完全本地运行", text)
        self.assertIn("不读取、不记录剪贴板正文", text)
        self.assertIn("清除全部本地数据", text)
        self.assertIn("Win+B", text)
        self.assertIn("Shift+F10", text)
        self.assertIn("设备感知默认开启", text)
        self.assertIn("nvidia-smi", text)
        self.assertIn("项目主页与反馈", text)
        self.assertIn("不包含开机自启", text)
        self.assertIn("每七枚", text)
        self.assertIn(APP_VERSION, labels)
        self.assertIn(str(DATA_DIR), text)
        dialog.close()

    def test_about_dialog_adapts_to_compact_high_dpi_work_area(self) -> None:
        compact_work_area = QRect(0, 0, 400, 400)
        with patch.object(
            self.window,
            "_window_screen_available",
            return_value=compact_work_area,
        ):
            self.window._show_about()

        dialog = self.window._about_dialog
        self.assertLessEqual(dialog.width(), compact_work_area.width() - 32)
        self.assertLessEqual(dialog.height(), compact_work_area.height() - 32)
        self.assertGreaterEqual(dialog.width(), 240)
        self.assertGreaterEqual(dialog.height(), 240)
        dialog.close()

    def test_about_dialog_refits_when_reopened_on_a_smaller_screen(self) -> None:
        large_work_area = QRect(0, 0, 1000, 800)
        compact_work_area = QRect(80, 40, 280, 220)
        with patch.object(
            self.window,
            "_window_screen_available",
            return_value=large_work_area,
        ):
            self.window._show_about()
        dialog = self.window._about_dialog
        self.assertEqual(dialog.size().width(), 620)
        self.assertEqual(dialog.size().height(), 600)
        dialog.close()

        with patch.object(
            self.window,
            "_window_screen_available",
            return_value=compact_work_area,
        ):
            self.window._show_about()
            self.app.processEvents()

        self.assertLessEqual(dialog.width(), compact_work_area.width() - 32)
        self.assertLessEqual(dialog.height(), compact_work_area.height() - 32)
        self.assertGreaterEqual(dialog.geometry().left(), compact_work_area.left())
        self.assertGreaterEqual(dialog.geometry().top(), compact_work_area.top())
        self.assertLessEqual(dialog.geometry().right(), compact_work_area.right())
        self.assertLessEqual(dialog.geometry().bottom(), compact_work_area.bottom())
        buttons = dialog.findChildren(QPushButton)
        self.assertEqual(len(buttons), 4)
        for button in buttons:
            with self.subTest(button=button.text()):
                self.assertTrue(dialog.rect().contains(button.geometry()))
        dialog.close()

    def test_main_window_exposes_an_accessible_identity(self) -> None:
        self.assertIn("月壳游灵", self.window.accessibleName())
        self.assertIn("右键", self.window.accessibleDescription())

    def test_app_icon_contains_real_tray_sizes(self) -> None:
        sizes = {size.width() for size in self.window._app_icon.availableSizes()}
        self.assertTrue({16, 20, 24, 32, 48, 64}.issubset(sizes))
        self.assertFalse(QApplication.windowIcon().isNull())

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

    def test_moon_phase_reaction_is_suppressed_without_consuming_marker_when_quiet_or_focusing(
        self,
    ) -> None:
        w = self.window
        saved = (w.settings.activity, w._focus_deadline)
        try:
            for mode in ("quiet", "focus"):
                with self.subTest(mode=mode):
                    w.settings.activity = "low" if mode == "quiet" else "high"
                    w._focus_deadline = (
                        0.0 if mode == "quiet" else time.monotonic() + 60.0
                    )
                    w._state.last_moon_event_key = "previous-event"
                    with (
                        patch("pet.pet_window.calculate_moon_phase") as calculate,
                        patch.object(w, "_save_state") as save_state,
                    ):
                        self.assertFalse(w._maybe_moon_phase_reaction())
                    calculate.assert_not_called()
                    save_state.assert_not_called()
                    self.assertEqual(
                        w._state.last_moon_event_key,
                        "previous-event",
                    )
        finally:
            w.settings.activity, w._focus_deadline = saved

    def test_principal_moon_phase_reaction_records_marker_with_one_save(self) -> None:
        w = self.window
        phase = MagicMock()
        phase.index = 4
        phase.principal_event_key.return_value = "principal-42"
        with (
            patch("pet.pet_window.calculate_moon_phase", return_value=phase),
            patch.object(w, "_react", return_value=True) as react,
            patch.object(w, "say") as say,
            patch.object(w, "_save_state", return_value=True) as save_state,
        ):
            self.assertTrue(w._maybe_moon_phase_reaction())

        react.assert_called_once_with("star", 2.8, "moon_phase", 6 * 3600)
        say.assert_called_once_with(
            "今天接近满月。抬头的时候，也许会想起我。",
            4.2,
        )
        save_state.assert_called_once_with(notify_failure=False)
        self.assertEqual(w._state.last_moon_event_key, "principal-42")

    def test_moon_phase_reaction_rolls_back_marker_when_save_fails(self) -> None:
        w = self.window
        w._state.last_moon_event_key = "previous-event"
        phase = MagicMock()
        phase.index = 2
        phase.principal_event_key.return_value = "principal-43"
        with (
            patch("pet.pet_window.calculate_moon_phase", return_value=phase),
            patch.object(w, "_react", return_value=True),
            patch.object(w, "say"),
            patch.object(w, "_save_state", return_value=False) as save_state,
        ):
            self.assertTrue(w._maybe_moon_phase_reaction())

        save_state.assert_called_once_with(notify_failure=False)
        self.assertEqual(w._state.last_moon_event_key, "previous-event")

    def test_non_principal_moon_phase_does_not_trigger_or_save(self) -> None:
        w = self.window
        w._state.last_moon_event_key = "previous-event"
        phase = MagicMock()
        phase.index = 1
        phase.principal_event_key.return_value = None
        with (
            patch("pet.pet_window.calculate_moon_phase", return_value=phase),
            patch.object(w, "_react") as react,
            patch.object(w, "say") as say,
            patch.object(w, "_save_state") as save_state,
        ):
            self.assertFalse(w._maybe_moon_phase_reaction())

        phase.principal_event_key.assert_called_once_with(within_days=0.75)
        react.assert_not_called()
        say.assert_not_called()
        save_state.assert_not_called()
        self.assertEqual(w._state.last_moon_event_key, "previous-event")

    def test_blocked_life_reaction_does_not_consume_daily_marker(self) -> None:
        w = self.window
        saved = (
            w._hour,
            w._greeted_morning_day,
            w._active_streak,
            w.action,
            w.walking,
        )
        try:
            w._hour = 7
            w._greeted_morning_day = -1
            w._active_streak = 0.0
            w.walking = False
            w.action = type(w.action)("read", time.monotonic() + 60, True)
            self.assertFalse(w._maybe_life_reactions(time.monotonic()))
            self.assertEqual(w._greeted_morning_day, -1)

            w._hour = 12
            w._active_streak = 50 * 60 + 1
            self.assertFalse(w._maybe_life_reactions(time.monotonic()))
            self.assertEqual(w._active_streak, 50 * 60 + 1)
        finally:
            (
                w._hour,
                w._greeted_morning_day,
                w._active_streak,
                w.action,
                w.walking,
            ) = saved

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

    def test_retired_dash_never_enters_the_active_walk_cycle(self) -> None:
        w = self.window
        saved_energy = w.mood.energy
        try:
            self.assertNotIn("dash", w.sprite_images)
            w._snap_to_taskbar(initial=True)
            min_x, max_x = w._wander_x_bounds()
            w.move(min_x, w.y())
            w.mood.energy = 1.0
            with patch("pet.pet_window.random.randint", return_value=max_x), \
                 patch("pet.pet_window.random.random", return_value=0.0):
                self.assertTrue(w._start_walk())
            self.assertFalse(w._dashing)
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

    def test_quick_tap_cancels_pending_teleport(self) -> None:
        w = self.window
        w._teleport_target = w.x() + 300
        point = self._character_point()
        global_point = w.mapToGlobal(point)
        w.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress,
            point,
            global_point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        ))
        w.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease,
            point,
            global_point,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        ))
        self.assertIsNone(w._teleport_target)

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
                w._maybe_notice_cursor(time.monotonic())
            self.assertEqual(w.action.name, "hide")
        finally:
            (w._cursor_was_near, w._last_glance, w._cursor_speed,
             w.mood.sleepiness, w.action.until) = saved

    def test_first_pat_of_the_day_offers_a_gift(self) -> None:
        w = self.window
        saved = (
            w._gift_day,
            w._state.last_gift_date,
            w._state.moon_tokens,
            w._pet_count,
            w._last_pet_t,
            w.action.until,
        )
        try:
            w._gift_day = -1
            w._state.last_gift_date = ""
            w._state.moon_tokens = 0
            w._pet_count = 0
            w._last_pet_t = 0.0
            w._on_pet()
            self.assertEqual(w.action.name, "gift")
            self.assertEqual(w._gift_day, time.localtime().tm_yday)
            self.assertEqual(w._state.moon_tokens, 1)
            self.assertTrue(w.act_today_gift.isEnabled())
            self.assertEqual(w.act_today_gift.text(), "重看今天的月光")

            w._on_pet()  # second pat, same day -> back to the normal pat ladder
            self.assertNotEqual(w.action.name, "gift")
            self.assertEqual(w._state.moon_tokens, 1)
        finally:
            (
                w._gift_day,
                w._state.last_gift_date,
                w._state.moon_tokens,
                w._pet_count,
                w._last_pet_t,
                w.action.until,
            ) = saved

    def test_seventh_daily_gift_becomes_a_milestone(self) -> None:
        w = self.window
        w._state.last_gift_date = ""
        w._state.moon_tokens = 6
        w._last_pet_t = 0.0
        w._on_pet()
        self.assertEqual(w._state.moon_tokens, 7)
        self.assertEqual(w.action.name, "crystal")
        self.assertIn("第 1 颗星晶", w.message)

    def test_clock_rollback_cannot_duplicate_daily_gift(self) -> None:
        w = self.window
        w._state.last_gift_date = "2999-12-31"
        w._state.moon_tokens = 8
        w._last_pet_t = 0.0
        w._on_pet()
        self.assertEqual(w._state.moon_tokens, 8)
        self.assertEqual(w.action.name, "curious")
        self.assertIn("明天会恢复", w.message)
        self.assertEqual(w._state.last_gift_date, time.strftime("%Y-%m-%d"))

    def test_future_gift_marker_self_heals_for_the_next_day(self) -> None:
        w = self.window
        w._state.last_gift_date = "2999-12-31"
        w._state.moon_tokens = 8

        with patch("pet.pet_window.time.strftime", return_value="2026-07-25"):
            w._show_today_gift()
        self.assertEqual(w._state.moon_tokens, 8)
        self.assertEqual(w._state.last_gift_date, "2026-07-25")
        self.assertEqual(w.act_today_gift.text(), "月光明天恢复")

        with patch("pet.pet_window.time.strftime", return_value="2026-07-26"):
            w._show_today_gift()
        self.assertEqual(w._state.moon_tokens, 9)
        self.assertEqual(w._state.last_gift_date, "2026-07-26")
        self.assertEqual(w.action.name, "gift")

    def test_daily_gift_rolls_back_when_memory_cannot_be_saved(self) -> None:
        w = self.window
        w._state.last_gift_date = ""
        w._state.moon_tokens = 0
        with patch.object(w._state, "save", return_value=False):
            w._show_today_gift()

        self.assertEqual(w._state.last_gift_date, "")
        self.assertEqual(w._state.moon_tokens, 0)
        self.assertTrue(w._gift_save_failed)
        self.assertTrue(w._state_save_failed)
        self.assertEqual(w.action.name, "curious")
        self.assertIn("暂时没有领取", w.message)
        self.assertIn("数据未保存", w.status_action.text())

    def test_focus_start_discloses_non_persistent_timer(self) -> None:
        w = self.window
        with patch.object(w._state, "save", return_value=False):
            w._start_focus(25)
        try:
            self.assertTrue(w._focus_active)
            self.assertTrue(w._state_save_failed)
            self.assertIn("无法保存", w.message)
            self.assertIn("退出后不会继续", w.message)
            self.assertIn("数据未保存", w.status_action.text())
        finally:
            w._cancel_focus()

    def test_today_gift_can_be_replayed_without_duplicating_it(self) -> None:
        w = self.window
        w._state.last_gift_date = time.strftime("%Y-%m-%d")
        w._state.moon_tokens = 4
        w._refresh_tray_status()
        w._show_today_gift()
        self.assertEqual(w.action.name, "gift")
        self.assertEqual(w._state.moon_tokens, 4)
        self.assertIn("4", w.message)

    def test_today_gift_menu_claims_directly_for_keyboard_users(self) -> None:
        w = self.window
        w._state.last_gift_date = ""
        w._state.moon_tokens = 0
        w._refresh_tray_status()
        self.assertEqual(w.act_today_gift.text(), "领取今日月光")
        with patch.object(w, "_save_state") as save_state:
            w._show_today_gift()
        self.assertEqual(w.action.name, "gift")
        self.assertEqual(w._state.moon_tokens, 1)
        self.assertEqual(w.act_today_gift.text(), "重看今天的月光")
        save_state.assert_called_once()

    def test_milestone_replay_keeps_crystal_exclusive(self) -> None:
        w = self.window
        w._state.last_gift_date = time.strftime("%Y-%m-%d")
        w._state.moon_tokens = 14
        w._refresh_tray_status()
        self.assertIn("月光 14 枚", w.tray.toolTip())
        self.assertIn("星晶 2 颗", w.tray.toolTip())
        w._show_today_gift()
        self.assertEqual(w.action.name, "crystal")
        self.assertIn("第 2 颗星晶", w.message)

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

    def test_speech_bubble_stays_inside_screen_at_both_edges(self) -> None:
        w = self.window
        saved = (w.message, w.message_until, w.pos())
        try:
            ag = w._window_screen_available()
            min_x, max_x = w._x_bounds(ag)
            w.say("我在屏幕边边也不会被裁掉。", 5.0, force=True)
            for x in (min_x, max_x):
                w.move(x, w._rest_y(ag))
                w.repaint()
                self.app.processEvents()
                rect = w._last_bubble_rect
                self.assertIsNotNone(rect)
                assert rect is not None
                global_left = w.x() + rect.left()
                global_right = w.x() + rect.right()
                self.assertGreaterEqual(global_left, ag.left())
                self.assertLessEqual(global_right, ag.right())
        finally:
            w.message, w.message_until = saved[0], saved[1]
            w.move(saved[2])
            w.update()

    def test_retired_side_glance_is_not_loaded_at_runtime(self) -> None:
        self.assertNotIn("look_side", self.window.sprite_images)
        self.assertNotIn("look_side_flip", self.window.sprite_images)

    def test_tray_menu_does_not_expose_monitoring_ui(self) -> None:
        labels = [
            action.text()
            for action in self.window.tray.contextMenu().actions()
            if not action.isSeparator()
        ]
        joined = " ".join(labels)
        for technical_term in ("实时状态", "CPU", "GPU", "显存", "内存"):
            self.assertNotIn(technical_term, joined)
        self.assertNotIn("显示调试边界", joined)
        self.assertIn("专注陪伴", joined)
        self.assertIn("感知与隐私", joined)
        self.assertIn("快速使用提示", joined)
        self.assertIn("使用与隐私", joined)
        self.assertIn("唤回到主屏幕", joined)

        privacy_text = " ".join(
            action.text() for action in self.window.awareness_menu.actions()
        )
        self.assertIn("完整隐私说明", privacy_text)

    def test_tray_choice_groups_are_exclusive(self) -> None:
        self.assertTrue(self.window.activity_group.isExclusive())
        self.assertTrue(self.window.size_group.isExclusive())
        self.assertIs(
            self.window.act_lively.actionGroup(),
            self.window.act_calm.actionGroup(),
        )
        self.assertIs(
            self.window.act_small.actionGroup(),
            self.window.act_standard.actionGroup(),
        )

    def test_stage_pixmap_cache_is_bounded(self) -> None:
        w = self.window
        old_limit = w.STAGE_CACHE_LIMIT
        try:
            w.STAGE_CACHE_LIMIT = 3
            w._stage_cache.clear()
            dpr = w._current_dpr()
            for name in ("idle", "blink", "happy", "curious"):
                w._build_stage_pixmap(name, dpr, 0, 0)
            self.assertEqual(len(w._stage_cache), 3)
            self.assertNotIn(("idle", round(dpr * 1000), 0, 0), w._stage_cache)
        finally:
            w.STAGE_CACHE_LIMIT = old_limit
            w._stage_cache.clear()

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

    def test_neutral_samples_decay_pressure_instead_of_accumulating(self) -> None:
        self._reset_resource_alert()
        w = self.window
        for cpu in (90, 80, 90, 80, 90, 80, 90):
            w._on_telemetry(self._telemetry(cpu=cpu, mem=40))
        self.assertFalse(w._resource_busy)
        self.assertLess(w._busy_samples, 4)

    def test_stale_queued_telemetry_is_ignored(self) -> None:
        self._reset_resource_alert()
        w = self.window
        before = w._telemetry_samples
        sample = self._telemetry(cpu=99, mem=99)
        sample.captured_at = time.monotonic() - 11.0
        w._on_telemetry(sample)
        self.assertEqual(w._telemetry_samples, before)
        self.assertFalse(w._resource_busy)

    def test_input_mask_expands_for_drag_and_contracts_after_release(self) -> None:
        w = self.window
        saved = (w.held, w.dragging, w.falling, w.debug_bounds)
        try:
            w.held = w.dragging = w.falling = w.debug_bounds = False
            w.message = ""
            w.message_until = 0.0
            w._input_mask_signature = None
            w._update_input_mask()
            self.assertTrue(w.mask().contains(self._character_point()))
            self.assertFalse(w.mask().contains(QPoint(1, 1)))

            w.dragging = True
            w._update_input_mask()
            self.assertTrue(w.mask().contains(QPoint(1, 1)))

            w.dragging = False
            w._update_input_mask()
            self.assertFalse(w.mask().contains(QPoint(1, 1)))
        finally:
            w.held, w.dragging, w.falling, w.debug_bounds = saved
            w._input_mask_signature = None
            w._update_input_mask()

    def test_companion_journal_reuses_dialog_after_work_area_change(self) -> None:
        w = self.window
        first_work_area = QRect(-1600, 0, 1600, 900)
        second_work_area = QRect(100, 50, 320, 240)
        with patch.object(
            w,
            "_window_screen_available",
            return_value=first_work_area,
        ):
            w._show_companion_journal()
        dialog = w._companion_journal_dialog
        dialog.close()

        with patch.object(
            w,
            "_window_screen_available",
            return_value=second_work_area,
        ):
            w._show_companion_journal()
            self.app.processEvents()

        self.assertIs(w._companion_journal_dialog, dialog)
        self.assertEqual(
            len(
                [
                    child
                    for child in w.findChildren(QDialog)
                    if "陪伴手账" in child.windowTitle()
                ]
            ),
            1,
        )
        geometry = dialog.geometry()
        self.assertGreaterEqual(geometry.left(), second_work_area.left())
        self.assertGreaterEqual(geometry.top(), second_work_area.top())
        self.assertLessEqual(geometry.right(), second_work_area.right())
        self.assertLessEqual(geometry.bottom(), second_work_area.bottom())
        for button in dialog.findChildren(QPushButton):
            with self.subTest(button=button.text()):
                self.assertTrue(dialog.rect().contains(button.geometry()))
        dialog.close()

    def test_daily_card_replace_failure_preserves_target_and_cleans_temp(
        self,
    ) -> None:
        w = self.window
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            target = directory / "existing.png"
            target.write_bytes(b"existing-card")
            with (
                patch(
                    "pet.pet_window.QFileDialog.getSaveFileName",
                    return_value=(str(target), "PNG 图片 (*.png)"),
                ),
                patch(
                    "pet.pet_window.os.replace",
                    side_effect=PermissionError("read only"),
                ) as replace,
                patch("pet.pet_window.QMessageBox.information") as information,
                patch("pet.pet_window.QMessageBox.warning") as warning,
            ):
                self.assertFalse(w._save_daily_card())

            replace.assert_called_once()
            information.assert_not_called()
            warning.assert_called_once()
            self.assertEqual(target.read_bytes(), b"existing-card")
            self.assertEqual(
                [
                    path.name
                    for path in directory.iterdir()
                    if path.name.endswith(".moonshell.tmp")
                ],
                [],
            )

    def test_shutdown_stops_all_timers_and_is_idempotent(self) -> None:
        w = self.window
        timers = (
            w.anim_timer,
            w.walk_timer,
            w.phys_timer,
            w.hover_timer,
            w.state_timer,
            w._display_timer,
            w._teleport_timer,
            w._action_sequence_timer,
            w._mask_timer,
            w._focus_timer,
            w._focus_status_timer,
            w._moon_focus_timer,
            w._tray_watchdog,
        )
        for timer in timers:
            timer.start(60_000)

        with (
            patch.object(w, "_save_position") as save_position,
            patch.object(w, "_save_state", return_value=True) as save_state,
            patch.object(
                w.monitor,
                "shutdown",
                wraps=w.monitor.shutdown,
            ) as monitor_shutdown,
            patch.object(w.tray, "hide", wraps=w.tray.hide) as hide_tray,
        ):
            w._shutdown()
            w._shutdown()

        self.assertTrue(w._shutdown_done)
        self.assertTrue(all(not timer.isActive() for timer in timers))
        save_position.assert_called_once()
        save_state.assert_called_once()
        monitor_shutdown.assert_called_once()
        hide_tray.assert_called_once()
        self.assertTrue(w.monitor.stopped)
        self.assertFalse(w.tray.isVisible())

    def test_blocked_moon_phase_reaction_retries_without_consuming_marker(
        self,
    ) -> None:
        w = self.window
        w._next_moon_phase_check = 0.0
        w._state.last_moon_event_key = "41"
        phase = MagicMock()
        phase.index = 4
        phase.principal_event_key.return_value = "42"
        with (
            patch("pet.pet_window.time.monotonic", return_value=1_000.0),
            patch("pet.pet_window.calculate_moon_phase", return_value=phase),
            patch.object(w, "_react", return_value=False) as react,
            patch.object(w, "_save_state") as save_state,
        ):
            self.assertFalse(w._maybe_moon_phase_reaction())

        react.assert_called_once_with("star", 2.8, "moon_phase", 6 * 3600)
        save_state.assert_not_called()
        self.assertEqual(w._next_moon_phase_check, 1_000.0 + 5 * 60)
        self.assertEqual(w._state.last_moon_event_key, "41")

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
