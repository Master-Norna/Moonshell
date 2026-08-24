from __future__ import annotations

import math
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QApplication

from pet.moon_corner import (
    MoonCornerSnapshot,
    MoonCornerWidget,
    MoonRenderer,
    growth_from_history,
)


class MoonCornerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        self.widgets: list[MoonCornerWidget] = []

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.shutdown()
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _widget(self, **kwargs: object) -> MoonCornerWidget:
        widget = MoonCornerWidget(**kwargs)
        self.widgets.append(widget)
        return widget

    def test_growth_uses_existing_tokens_and_completed_focus_minutes(self) -> None:
        cases = (
            (0, 0, (0, 0.0)),
            (7, 0, (1, 0.0)),
            (0, 175, (1, 0.0)),
            (6, 25, (1, 0.0)),
            (1, 25, (0, 2.0 / 7.0)),
        )
        for tokens, minutes, expected in cases:
            with self.subTest(tokens=tokens, minutes=minutes):
                level, progress = growth_from_history(tokens, minutes)
                self.assertEqual(level, expected[0])
                self.assertAlmostEqual(progress, expected[1])

        snapshot = MoonCornerSnapshot(
            illumination=float("nan"),
            phase_index=11,
            moon_tokens=-4,
            focus_minutes_completed=-25,
            focus_progress=float("inf"),
            growth_level=-2,
            growth_progress=2.5,
            screen_geometry=QRect(),
        ).normalized()
        self.assertEqual(snapshot.illumination, 0.0)
        self.assertEqual(snapshot.phase_index, 3)
        self.assertEqual(snapshot.moon_tokens, 0)
        self.assertEqual(snapshot.focus_minutes_completed, 0)
        self.assertEqual(snapshot.focus_progress, 0.0)
        self.assertEqual(snapshot.resolved_growth, (0, 1.0))
        self.assertIsNone(snapshot.screen_geometry)

    def test_renderer_supports_multiple_sizes_and_device_pixel_ratios(self) -> None:
        snapshot = MoonCornerSnapshot(
            illumination=0.73,
            phase_index=3,
            moon_tokens=16,
            focus_minutes_completed=125,
            focus_active=True,
            focus_progress=0.46,
        )
        cases = (
            (QSize(64, 64), 1.0),
            (QSize(176, 176), 1.5),
            (QSize(420, 260), 2.0),
        )
        for logical_size, dpr in cases:
            with self.subTest(size=logical_size, dpr=dpr):
                image = MoonRenderer.render_image(
                    logical_size,
                    snapshot,
                    device_pixel_ratio=dpr,
                )
                self.assertFalse(image.isNull())
                self.assertTrue(image.hasAlphaChannel())
                self.assertEqual(image.devicePixelRatio(), dpr)
                self.assertEqual(
                    image.size(),
                    QSize(
                        round(logical_size.width() * dpr),
                        round(logical_size.height() * dpr),
                    ),
                )
                center = image.pixelColor(image.width() // 2, image.height() // 2)
                self.assertGreater(center.alpha(), 0)

        # The shared phase shape must remain finite at both extrema.
        for illumination in (0.0, 1.0, float("nan")):
            path = MoonRenderer.illuminated_path(
                QPoint(20, 20),
                15.0,
                illumination,
                4,
            )
            self.assertTrue(
                path.isEmpty()
                or (
                    math.isfinite(path.boundingRect().width())
                    and math.isfinite(path.boundingRect().height())
                )
            )

    def test_anchor_respects_negative_screen_origins_and_each_corner(self) -> None:
        widget = self._widget(size=QSize(176, 176))
        screen = QRect(-1920, -100, 1920, 1080)

        top_right = widget.anchor_to_screen(
            screen,
            corner="top_right",
            margin=(12, 8),
        )
        self.assertEqual(top_right, QRect(-188, -92, 176, 176))

        bottom_left = widget.anchor_to_screen(
            screen,
            corner="bottom_left",
            margin=QPoint(12, 8),
        )
        self.assertEqual(bottom_left, QRect(-1908, 796, 176, 176))

        with self.assertRaises(ValueError):
            widget.anchor_to_screen(screen, corner="middle")  # type: ignore[arg-type]

    def test_default_window_is_passive_and_interaction_is_explicit(self) -> None:
        widget = self._widget()
        flags = widget.windowFlags()
        self.assertTrue(flags & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowType.Tool)
        self.assertTrue(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.WindowType.WindowTransparentForInput)
        self.assertTrue(
            widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        )
        self.assertFalse(widget.interactive)

        widget.set_interactive(True)
        self.assertTrue(widget.interactive)
        self.assertFalse(
            widget.windowFlags() & Qt.WindowType.WindowTransparentForInput
        )
        self.assertFalse(
            widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        )

        widget.set_interactive(False)
        self.assertFalse(widget.interactive)
        self.assertTrue(
            widget.windowFlags() & Qt.WindowType.WindowTransparentForInput
        )

    def test_always_on_top_toggle_preserves_visibility_and_anchor(self) -> None:
        widget = self._widget()
        anchored = widget.anchor_to_screen(
            QRect(40, 60, 1280, 720),
            corner="top_right",
            margin=14,
        )
        widget.show()
        self.app.processEvents()
        self.assertTrue(widget.isVisible())
        self.assertTrue(widget.always_on_top)

        widget.set_always_on_top(False)
        self.app.processEvents()
        self.assertTrue(widget.isVisible())
        self.assertFalse(widget.always_on_top)
        self.assertEqual(widget.geometry(), anchored)

        # Both the no-op and re-enable paths preserve the native window state.
        widget.set_always_on_top(False)
        self.assertTrue(widget.isVisible())
        widget.set_always_on_top(True)
        self.app.processEvents()
        self.assertTrue(widget.isVisible())
        self.assertTrue(widget.always_on_top)
        self.assertEqual(widget.geometry(), anchored)

    def test_pulse_is_bounded_and_shutdown_is_idempotent(self) -> None:
        widget = self._widget()
        widget.show()
        self.app.processEvents()

        finished: list[str] = []
        widget.pulse_finished.connect(finished.append)

        widget.pulse("focus_complete")
        self.assertTrue(widget._pulse_timer.isActive())
        self.assertEqual(widget._pulse_kind, "focus_complete")
        widget._pulse_started -= widget._pulse_duration + 0.01
        widget._advance_pulse()
        self.assertFalse(widget._pulse_timer.isActive())
        self.assertEqual(widget._pulse_kind, "")
        self.assertEqual(finished, ["focus_complete"])

        widget.pulse("gift")
        self.assertTrue(widget._pulse_timer.isActive())

        widget.shutdown()
        self.assertFalse(widget.isVisible())
        self.assertFalse(widget._pulse_timer.isActive())
        self.assertEqual(widget._pulse_kind, "")

        widget.shutdown()
        widget.pulse("gift")
        self.assertFalse(widget._pulse_timer.isActive())
        self.assertEqual(widget._pulse_kind, "")


if __name__ == "__main__":
    unittest.main()
