"""A quiet, state-driven moon that can live in a desktop corner.

The widget deliberately owns no product logic.  It only renders a snapshot
provided by the companion window and exposes an opt-in activation signal.  In
particular, it does not start focus sessions, mutate :class:`PetState`, or open
its own journal.  The host can connect :attr:`MoonCornerWidget.activated` to
the existing journal action when click interaction is desired.

``MoonRenderer`` is independent from the top-level widget so the same visual
language can be reused by the daily card (or any other QPainter target).
Qt coordinates are device-independent; Qt applies the target device pixel
ratio when painting the widget or an image returned by ``render_image``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Final, Literal

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QGuiApplication,
    QHideEvent,
    QImage,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
    QScreen,
    QShowEvent,
)
from PySide6.QtWidgets import QWidget


FOCUS_MINUTES_PER_GROWTH_UNIT: Final = 25.0
GROWTH_UNITS_PER_LEVEL: Final = 7.0
DEFAULT_CORNER_SIZE: Final = QSize(176, 176)

AnchorCorner = Literal["top_right", "top_left", "bottom_right", "bottom_left"]


def _finite_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _nonnegative_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, result)


def growth_from_history(
    moon_tokens: object,
    focus_minutes_completed: object,
) -> tuple[int, float]:
    """Return ``(level, progress)`` from the two existing continuity values.

    One completed 25-minute focus block contributes the same growth as one
    moon token.  Seven growth units form a permanent level, preserving the
    existing seven-token/star-crystal rhythm without introducing new storage.
    Fractional focus time is retained, so every completed minute can make a
    small visual contribution.
    """

    tokens = _nonnegative_int(moon_tokens)
    minutes = _finite_float(focus_minutes_completed, 0.0)
    minutes = max(0.0, minutes)
    score = tokens + minutes / FOCUS_MINUTES_PER_GROWTH_UNIT
    level = int(score // GROWTH_UNITS_PER_LEVEL)
    progress = (score - level * GROWTH_UNITS_PER_LEVEL) / GROWTH_UNITS_PER_LEVEL
    return level, min(1.0, max(0.0, progress))


@dataclass(frozen=True, slots=True)
class MoonCornerSnapshot:
    """All state needed to paint the moon, with no ownership of that state.

    ``illumination`` and ``phase_index`` should come from ``MoonPhase``.
    ``growth_level`` and ``growth_progress`` are optional overrides; when they
    are omitted they are derived from ``moon_tokens`` and
    ``focus_minutes_completed`` by :func:`growth_from_history`.
    """

    illumination: float = 0.0
    phase_index: int = 0
    moon_tokens: int = 0
    focus_minutes_completed: int = 0
    focus_active: bool = False
    focus_progress: float = 0.0
    growth_level: int | None = None
    growth_progress: float | None = None
    screen_geometry: QRect | None = None

    @classmethod
    def from_phase(cls, phase: object, **state: object) -> "MoonCornerSnapshot":
        """Build a snapshot from any object exposing ``illumination``/``index``."""

        try:
            illumination = getattr(phase, "illumination")
            phase_index = getattr(phase, "index")
        except AttributeError as exc:
            raise TypeError("phase must expose illumination and index") from exc
        return cls(
            illumination=illumination,
            phase_index=phase_index,
            **state,
        ).normalized()

    @property
    def resolved_growth(self) -> tuple[int, float]:
        derived_level, derived_progress = growth_from_history(
            self.moon_tokens,
            self.focus_minutes_completed,
        )
        level = (
            derived_level
            if self.growth_level is None
            else _nonnegative_int(self.growth_level)
        )
        progress = (
            derived_progress
            if self.growth_progress is None
            else _finite_float(self.growth_progress, derived_progress)
        )
        return level, min(1.0, max(0.0, progress))

    def normalized(self) -> "MoonCornerSnapshot":
        """Return a finite, paint-safe copy while preserving override intent."""

        illumination = min(1.0, max(0.0, _finite_float(self.illumination, 0.0)))
        phase_index = _nonnegative_int(self.phase_index) % 8
        moon_tokens = min(1_000_000, _nonnegative_int(self.moon_tokens))
        focus_minutes = min(
            10_000_000,
            _nonnegative_int(self.focus_minutes_completed),
        )
        focus_progress = min(
            1.0,
            max(0.0, _finite_float(self.focus_progress, 0.0)),
        )
        growth_level = (
            None
            if self.growth_level is None
            else min(1_000_000, _nonnegative_int(self.growth_level))
        )
        growth_progress = (
            None
            if self.growth_progress is None
            else min(
                1.0,
                max(0.0, _finite_float(self.growth_progress, 0.0)),
            )
        )
        geometry = self.screen_geometry
        if isinstance(geometry, QRect) and geometry.isValid() and not geometry.isEmpty():
            geometry = QRect(geometry)
        else:
            geometry = None
        return replace(
            self,
            illumination=illumination,
            phase_index=phase_index,
            moon_tokens=moon_tokens,
            focus_minutes_completed=focus_minutes,
            focus_active=bool(self.focus_active),
            focus_progress=focus_progress,
            growth_level=growth_level,
            growth_progress=growth_progress,
            screen_geometry=geometry,
        )


class MoonRenderer:
    """Reusable painter for the corner moon and larger card-sized variants."""

    # Normalized positions are deliberately fixed: growth feels continuous and
    # does not make the desktop flicker by reshuffling decoration each update.
    _STARS: Final = (
        (0.176, 0.280, 1.00),
        (0.818, 0.205, 0.76),
        (0.884, 0.445, 0.92),
        (0.735, 0.790, 0.68),
        (0.288, 0.820, 0.82),
        (0.105, 0.568, 0.62),
        (0.452, 0.116, 0.64),
        (0.925, 0.660, 0.56),
        (0.560, 0.900, 0.58),
        (0.075, 0.390, 0.52),
        (0.695, 0.090, 0.48),
        (0.410, 0.930, 0.46),
    )

    _MOTES: Final = (
        (0.31, 0.18, 0.58),
        (0.76, 0.32, 0.43),
        (0.20, 0.68, 0.36),
        (0.83, 0.72, 0.46),
    )

    @staticmethod
    def _disc_path(center: QPointF, radius: float) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(center, radius, radius)
        return path

    @staticmethod
    def illuminated_path(
        center: QPointF,
        radius: float,
        illumination: float,
        phase_index: int,
    ) -> QPainterPath:
        """Return a smooth lit shape whose area follows ``illumination``.

        Indexes 0..4 illuminate from the right (waxing) and 5..7 from the left
        (waning).  The terminator is sampled as an ellipse, which remains clean
        at both widget and 1080px card scale.
        """

        fraction = min(1.0, max(0.0, _finite_float(illumination, 0.0)))
        if radius <= 0.0 or fraction <= 0.0005:
            return QPainterPath()
        if fraction >= 0.9995:
            return MoonRenderer._disc_path(center, radius)

        waxing = _nonnegative_int(phase_index) % 8 <= 4
        limb: list[QPointF] = []
        terminator: list[QPointF] = []
        samples = 64
        for sample in range(samples + 1):
            normalized_y = -1.0 + 2.0 * sample / samples
            half_width = math.sqrt(max(0.0, 1.0 - normalized_y * normalized_y))
            y = center.y() + normalized_y * radius
            if waxing:
                outer_x = center.x() + half_width * radius
                inner_x = center.x() + (1.0 - 2.0 * fraction) * half_width * radius
            else:
                outer_x = center.x() - half_width * radius
                inner_x = center.x() + (2.0 * fraction - 1.0) * half_width * radius
            limb.append(QPointF(outer_x, y))
            terminator.append(QPointF(inner_x, y))

        polygon = QPolygonF(limb + list(reversed(terminator)))
        path = QPainterPath()
        path.addPolygon(polygon)
        path.closeSubpath()
        return path

    @staticmethod
    def _star_path(center: QPointF, radius: float) -> QPainterPath:
        path = QPainterPath()
        for point_index in range(8):
            angle = -math.pi / 2.0 + point_index * math.pi / 4.0
            point_radius = radius if point_index % 2 == 0 else radius * 0.24
            point = QPointF(
                center.x() + math.cos(angle) * point_radius,
                center.y() + math.sin(angle) * point_radius,
            )
            if point_index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        path.closeSubpath()
        return path

    @classmethod
    def paint(
        cls,
        painter: QPainter,
        bounds: QRect | QRectF,
        snapshot: MoonCornerSnapshot,
        *,
        pulse_progress: float | None = None,
        pulse_kind: str = "gentle",
    ) -> None:
        """Paint into ``bounds`` without beginning or ending ``painter``."""

        state = snapshot.normalized()
        rect = QRectF(bounds)
        extent = min(rect.width(), rect.height())
        if extent < 8.0:
            return

        growth_level, growth_progress = state.resolved_growth
        growth_steps = growth_level + growth_progress
        maturity = 1.0 - math.exp(-growth_steps / 5.5)
        pulse = (
            -1.0
            if pulse_progress is None
            else min(1.0, max(0.0, _finite_float(pulse_progress, 0.0)))
        )
        pulse_wave = math.sin(math.pi * pulse) if pulse >= 0.0 else 0.0

        center = QPointF(
            rect.center().x(),
            rect.center().y() - extent * 0.015,
        )
        radius = extent * (0.218 + 0.035 * maturity)
        radius *= 1.0 + 0.025 * pulse_wave
        halo_radius = radius * (1.62 + 0.16 * maturity + 0.08 * pulse_wave)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setPen(Qt.PenStyle.NoPen)

        # A layered radial wash keeps the edge translucent instead of looking
        # like a sticker.  Growth increases presence, never the lunar phase.
        halo = QRadialGradient(center, halo_radius)
        halo_alpha = int(18 + 26 * maturity + 25 * pulse_wave)
        halo.setColorAt(0.0, QColor(255, 213, 111, halo_alpha))
        halo.setColorAt(0.42, QColor(205, 210, 255, int(halo_alpha * 0.44)))
        halo.setColorAt(0.78, QColor(152, 159, 202, int(halo_alpha * 0.12)))
        halo.setColorAt(1.0, QColor(152, 159, 202, 0))
        painter.setBrush(halo)
        painter.drawEllipse(center, halo_radius, halo_radius)

        # Barely visible dust gives even a new moon a place in the scene.  It
        # is environmental texture; the larger connected stars below are the
        # persistent growth marks.
        for x_ratio, y_ratio, strength in cls._MOTES:
            mote = QPointF(
                rect.left() + x_ratio * rect.width(),
                rect.top() + y_ratio * rect.height(),
            )
            mote_radius = max(0.55, extent * 0.004 * strength)
            painter.setBrush(QColor(223, 224, 255, int(26 + 20 * strength)))
            painter.drawEllipse(mote, mote_radius, mote_radius)

        permanent_stars = min(len(cls._STARS), growth_level)
        emerging_star = permanent_stars < len(cls._STARS) and growth_progress > 0.015
        visible_stars = permanent_stars + (1 if emerging_star else 0)
        star_points = [
            QPointF(
                rect.left() + cls._STARS[index][0] * rect.width(),
                rect.top() + cls._STARS[index][1] * rect.height(),
            )
            for index in range(visible_stars)
        ]

        if permanent_stars >= 2:
            connection_alpha = int(24 + 22 * maturity)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    QColor(152, 159, 202, connection_alpha),
                    max(0.55, extent * 0.0042),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            for index in range(1, permanent_stars):
                painter.drawLine(star_points[index - 1], star_points[index])
            painter.setPen(Qt.PenStyle.NoPen)

        for index, point in enumerate(star_points):
            _x, _y, strength = cls._STARS[index]
            completion = 1.0 if index < permanent_stars else growth_progress
            star_radius = extent * (0.010 + 0.006 * strength) * (0.55 + 0.45 * completion)
            alpha = int((72 + 128 * strength) * (0.25 + 0.75 * completion))
            glow_radius = star_radius * 2.4
            star_glow = QRadialGradient(point, glow_radius)
            star_glow.setColorAt(0.0, QColor(255, 235, 175, int(alpha * 0.45)))
            star_glow.setColorAt(1.0, QColor(255, 224, 150, 0))
            painter.setBrush(star_glow)
            painter.drawEllipse(point, glow_radius, glow_radius)
            painter.setBrush(QColor(255, 239, 192, alpha))
            painter.drawPath(cls._star_path(point, star_radius))

        # Focus is deliberately a ring, not a second dashboard.  The host owns
        # the clock and pushes progress, so there is no idle polling here.
        if state.focus_active:
            ring_radius = radius + extent * 0.056
            ring_rect = QRectF(
                center.x() - ring_radius,
                center.y() - ring_radius,
                ring_radius * 2.0,
                ring_radius * 2.0,
            )
            ring_width = max(1.1, extent * 0.009)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    QColor(152, 159, 202, 46),
                    ring_width,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawEllipse(ring_rect)
            if state.focus_progress > 0.001:
                painter.setPen(
                    QPen(
                        QColor(205, 210, 255, 184),
                        ring_width,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                    )
                )
                painter.drawArc(
                    ring_rect,
                    90 * 16,
                    -round(state.focus_progress * 360.0 * 16.0),
                )
                angle = -math.pi / 2.0 + state.focus_progress * math.tau
                cap = QPointF(
                    center.x() + math.cos(angle) * ring_radius,
                    center.y() + math.sin(angle) * ring_radius,
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 243, 199, 220))
                painter.drawEllipse(cap, ring_width * 0.72, ring_width * 0.72)

        if pulse >= 0.0:
            pulse_radius = radius + extent * (0.045 + 0.105 * pulse)
            if pulse_kind in {"focus", "focus_complete", "complete"}:
                pulse_color = QColor(205, 210, 255, int(150 * (1.0 - pulse)))
            elif pulse_kind in {"gift", "token", "moon_token"}:
                pulse_color = QColor(255, 221, 143, int(154 * (1.0 - pulse)))
            else:
                pulse_color = QColor(210, 205, 255, int(120 * (1.0 - pulse)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    pulse_color,
                    max(0.85, extent * 0.008 * (1.0 - 0.45 * pulse)),
                    Qt.PenStyle.SolidLine,
                )
            )
            painter.drawEllipse(center, pulse_radius, pulse_radius)

        disc = cls._disc_path(center, radius)
        shadow = QRadialGradient(
            QPointF(center.x() - radius * 0.30, center.y() - radius * 0.34),
            radius * 1.55,
        )
        shadow.setColorAt(0.0, QColor(36, 42, 83, 234))
        shadow.setColorAt(0.58, QColor(22, 25, 54, 245))
        shadow.setColorAt(1.0, QColor(9, 10, 27, 252))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawPath(disc)

        lit = cls.illuminated_path(
            center,
            radius,
            state.illumination,
            state.phase_index,
        )
        if not lit.isEmpty():
            light = QRadialGradient(
                QPointF(center.x() - radius * 0.30, center.y() - radius * 0.38),
                radius * 1.55,
            )
            light_boost = int(8 * maturity + 20 * pulse_wave)
            light.setColorAt(0.0, QColor(255, 250, 219, 246))
            light.setColorAt(0.45, QColor(245, 226, 176, 242))
            light.setColorAt(0.82, QColor(212, 184, 128, 238 + light_boost // 5))
            light.setColorAt(1.0, QColor(148, 120, 84, 242))
            painter.setBrush(light)
            painter.drawPath(lit)

            # Restrained surface variation reads at both 176px and card scale.
            painter.save()
            painter.setClipPath(lit, Qt.ClipOperation.IntersectClip)
            craters = (
                (-0.28, -0.18, 0.105, 22),
                (0.23, 0.12, 0.145, 18),
                (-0.02, 0.31, 0.075, 20),
                (0.19, -0.35, 0.055, 15),
            )
            for x_ratio, y_ratio, size_ratio, alpha in craters:
                crater_center = QPointF(
                    center.x() + radius * x_ratio,
                    center.y() + radius * y_ratio,
                )
                crater_radius = radius * size_ratio
                painter.setBrush(QColor(104, 83, 72, alpha))
                painter.drawEllipse(crater_center, crater_radius, crater_radius * 0.72)
                painter.setBrush(QColor(255, 248, 218, max(6, alpha // 2)))
                painter.drawEllipse(
                    QPointF(
                        crater_center.x() - crater_radius * 0.16,
                        crater_center.y() - crater_radius * 0.18,
                    ),
                    crater_radius * 0.70,
                    crater_radius * 0.45,
                )
            painter.restore()

        # The outline is intentionally dim on the shadow side: enough to keep
        # the new moon legible on varied wallpapers, never a bright badge.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                QColor(239, 224, 191, int(42 + 34 * state.illumination)),
                max(0.75, extent * 0.006),
            )
        )
        painter.drawPath(disc)
        painter.restore()

    @classmethod
    def render_image(
        cls,
        size: QSize,
        snapshot: MoonCornerSnapshot,
        *,
        device_pixel_ratio: float = 1.0,
    ) -> QImage:
        """Render the shared visual into a transparent, high-DPI-aware image."""

        logical_size = QSize(max(1, size.width()), max(1, size.height()))
        dpr = _finite_float(device_pixel_ratio, 1.0)
        dpr = min(8.0, max(1.0, dpr))
        pixel_size = QSize(
            max(1, round(logical_size.width() * dpr)),
            max(1, round(logical_size.height() * dpr)),
        )
        image = QImage(pixel_size, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        cls.paint(
            painter,
            QRectF(0.0, 0.0, logical_size.width(), logical_size.height()),
            snapshot,
        )
        painter.end()
        return image


class MoonCornerWidget(QWidget):
    """Transparent, frameless, always-on-top presenter for ``MoonRenderer``.

    Mouse input passes through by default.  Call ``set_interactive(True)`` and
    connect ``activated`` to the host's existing journal action to opt in.
    """

    activated = Signal()
    context_requested = Signal(QPoint)
    pulse_finished = Signal(str)

    _PULSE_DURATIONS: Final = {
        "gentle": 0.72,
        "journal": 0.64,
        "pet": 0.64,
        "gift": 0.92,
        "token": 0.92,
        "moon_token": 0.92,
        "focus": 1.08,
        "focus_complete": 1.18,
        "complete": 1.18,
        "phase": 1.00,
        "moon_phase": 1.00,
    }

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        snapshot: MoonCornerSnapshot | None = None,
        interactive: bool = False,
        size: QSize = DEFAULT_CORNER_SIZE,
    ) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if not interactive:
            flags |= Qt.WindowType.WindowTransparentForInput
        super().__init__(parent, flags)

        self._snapshot = (snapshot or MoonCornerSnapshot()).normalized()
        self._interactive = bool(interactive)
        self._anchor_corner: AnchorCorner = "top_right"
        self._anchor_margin = QPoint(18, 18)
        self._anchor_geometry: QRect | None = None
        self._anchor_screen: QScreen | None = None
        self._closing = False
        self._pulse_kind = ""
        self._pulse_started = 0.0
        self._pulse_duration = 0.0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)
        self._pulse_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._pulse_timer.timeout.connect(self._advance_pulse)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            not self._interactive,
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("moonCorner")
        self.setAccessibleName("月亮手账")
        self.setFixedSize(
            max(96, size.width()),
            max(96, size.height()),
        )
        if self._interactive:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        if self._snapshot.screen_geometry is not None:
            self.anchor_to_screen(self._snapshot.screen_geometry)

    @property
    def snapshot(self) -> MoonCornerSnapshot:
        return self._snapshot

    @property
    def interactive(self) -> bool:
        return self._interactive

    @property
    def always_on_top(self) -> bool:
        """Whether the native window is currently configured to stay on top."""

        return bool(
            self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        )

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt virtual name
        return QSize(DEFAULT_CORNER_SIZE)

    @staticmethod
    def _mapping_snapshot(values: Mapping[str, object]) -> MoonCornerSnapshot:
        data = dict(values)
        if "index" in data:
            if "phase_index" in data:
                raise TypeError("use either index or phase_index, not both")
            data["phase_index"] = data.pop("index")
        valid_names = {field.name for field in fields(MoonCornerSnapshot)}
        unknown = sorted(set(data) - valid_names)
        if unknown:
            raise TypeError(f"unknown moon snapshot field(s): {', '.join(unknown)}")
        return MoonCornerSnapshot(**data).normalized()

    def set_snapshot(
        self,
        snapshot: MoonCornerSnapshot | Mapping[str, object],
    ) -> None:
        """Replace the complete visual snapshot and repaint if it changed."""

        if isinstance(snapshot, MoonCornerSnapshot):
            resolved = snapshot.normalized()
        elif isinstance(snapshot, Mapping):
            resolved = self._mapping_snapshot(snapshot)
        else:
            raise TypeError("snapshot must be MoonCornerSnapshot or a mapping")

        changed = resolved != self._snapshot
        geometry_changed = resolved.screen_geometry != self._snapshot.screen_geometry
        self._snapshot = resolved
        if geometry_changed and resolved.screen_geometry is not None:
            self.anchor_to_screen(resolved.screen_geometry)
        if changed:
            self.update()

    def update_state(self, **changes: object) -> None:
        """Update selected snapshot fields without introducing another state owner."""

        if "index" in changes:
            if "phase_index" in changes:
                raise TypeError("use either index or phase_index, not both")
            changes["phase_index"] = changes.pop("index")
        valid_names = {field.name for field in fields(MoonCornerSnapshot)}
        unknown = sorted(set(changes) - valid_names)
        if unknown:
            raise TypeError(f"unknown moon snapshot field(s): {', '.join(unknown)}")
        self.set_snapshot(replace(self._snapshot, **changes))

    def set_interactive(self, enabled: bool) -> None:
        """Opt in to clicks; disabled is a click-through desktop ornament."""

        enabled = bool(enabled)
        if enabled == self._interactive:
            return
        self._interactive = enabled
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            not enabled,
        )
        self._set_window_flag_preserving_visibility(
            Qt.WindowType.WindowTransparentForInput,
            not enabled,
        )
        if enabled:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()

    def _set_window_flag_preserving_visibility(
        self,
        flag: Qt.WindowType,
        enabled: bool,
    ) -> None:
        """Change one native flag without losing the widget's visible state."""

        enabled = bool(enabled)
        if bool(self.windowFlags() & flag) == enabled:
            return
        was_visible = self.isVisible()
        self.setWindowFlag(flag, enabled)
        # Changing a top-level flag recreates or hides the native window on
        # some platforms.  WA_ShowWithoutActivating keeps this restoration
        # passive, and reanchoring prevents a window-manager placement jump.
        if was_visible and not self._closing:
            self.show()
            self._reanchor()

    def set_always_on_top(self, enabled: bool) -> None:
        """Toggle stay-on-top while preserving visibility and screen anchor."""

        if self._closing:
            return
        self._set_window_flag_preserving_visibility(
            Qt.WindowType.WindowStaysOnTopHint,
            bool(enabled),
        )

    def _disconnect_anchor_screen(self) -> None:
        if self._anchor_screen is None:
            return
        try:
            self._anchor_screen.availableGeometryChanged.disconnect(
                self._on_screen_geometry_changed
            )
        except (RuntimeError, TypeError):
            pass
        self._anchor_screen = None

    def _set_anchor_screen(self, screen: QScreen | None) -> None:
        if screen is self._anchor_screen:
            return
        self._disconnect_anchor_screen()
        self._anchor_screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._on_screen_geometry_changed)

    def _screen_for_widget(self) -> QScreen | None:
        current = self.screen()
        if current is not None:
            return current
        application = QGuiApplication.instance()
        return application.primaryScreen() if application is not None else None

    @staticmethod
    def _margin_point(margin: int | tuple[int, int] | QPoint) -> QPoint:
        if isinstance(margin, QPoint):
            return QPoint(max(0, margin.x()), max(0, margin.y()))
        if isinstance(margin, tuple) and len(margin) == 2:
            return QPoint(
                max(0, _nonnegative_int(margin[0])),
                max(0, _nonnegative_int(margin[1])),
            )
        value = _nonnegative_int(margin, 18)
        return QPoint(value, value)

    def anchor_to_screen(
        self,
        target: QScreen | QRect | None = None,
        *,
        corner: AnchorCorner = "top_right",
        margin: int | tuple[int, int] | QPoint = 18,
    ) -> QRect:
        """Anchor inside a screen's available geometry and return widget geometry."""

        if corner not in {"top_right", "top_left", "bottom_right", "bottom_left"}:
            raise ValueError(f"unsupported corner: {corner!r}")
        self._anchor_corner = corner
        self._anchor_margin = self._margin_point(margin)

        if isinstance(target, QScreen):
            self._set_anchor_screen(target)
            geometry = target.availableGeometry()
            self._anchor_geometry = None
        elif isinstance(target, QRect):
            self._set_anchor_screen(None)
            geometry = QRect(target)
            self._anchor_geometry = QRect(target)
        elif target is None:
            screen = self._screen_for_widget()
            self._set_anchor_screen(screen)
            geometry = screen.availableGeometry() if screen is not None else QRect()
            self._anchor_geometry = None
        else:
            raise TypeError("target must be QScreen, QRect, or None")

        if not geometry.isValid() or geometry.isEmpty():
            return self.geometry()
        self._move_to_anchor(geometry)
        return self.geometry()

    def _move_to_anchor(self, geometry: QRect) -> None:
        margin_x = self._anchor_margin.x()
        margin_y = self._anchor_margin.y()
        if self._anchor_corner.endswith("right"):
            x = geometry.right() - self.width() + 1 - margin_x
        else:
            x = geometry.left() + margin_x
        if self._anchor_corner.startswith("bottom"):
            y = geometry.bottom() - self.height() + 1 - margin_y
        else:
            y = geometry.top() + margin_y

        max_x = max(geometry.left(), geometry.right() - self.width() + 1)
        max_y = max(geometry.top(), geometry.bottom() - self.height() + 1)
        x = min(max_x, max(geometry.left(), x))
        y = min(max_y, max(geometry.top(), y))
        self.move(x, y)

    def _reanchor(self) -> None:
        if self._anchor_geometry is not None:
            self._move_to_anchor(self._anchor_geometry)
        elif self._anchor_screen is not None:
            self._move_to_anchor(self._anchor_screen.availableGeometry())

    def _on_screen_geometry_changed(self, geometry: QRect) -> None:
        if not self._closing:
            self._move_to_anchor(geometry)

    def pulse(self, kind: str = "gentle") -> None:
        """Play a short, bounded acknowledgement with no persistent timer."""

        if self._closing:
            return
        normalized_kind = str(kind or "gentle").strip().lower()
        duration = self._PULSE_DURATIONS.get(normalized_kind, 0.78)
        self._pulse_kind = normalized_kind
        self._pulse_started = time.monotonic()
        self._pulse_duration = duration
        if self.isVisible() and not self._closing:
            self._pulse_timer.start()
        self.update()

    def _pulse_progress(self) -> float | None:
        if not self._pulse_kind or self._pulse_duration <= 0.0:
            return None
        return (time.monotonic() - self._pulse_started) / self._pulse_duration

    def _clear_pulse(self, *, emit_finished: bool) -> None:
        kind = self._pulse_kind
        self._pulse_timer.stop()
        self._pulse_kind = ""
        self._pulse_started = 0.0
        self._pulse_duration = 0.0
        if emit_finished and kind:
            self.pulse_finished.emit(kind)

    def _advance_pulse(self) -> None:
        progress = self._pulse_progress()
        if progress is None:
            self._pulse_timer.stop()
        elif progress >= 1.0:
            self._clear_pulse(emit_finished=True)
            self.update()
        elif not self.isVisible() or self._closing:
            self._pulse_timer.stop()
        else:
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt virtual name
        del event
        pulse_progress = self._pulse_progress()
        if pulse_progress is not None and pulse_progress >= 1.0:
            # Avoid emitting a signal from paintEvent; the timer owns completion.
            pulse_progress = None
        painter = QPainter(self)
        MoonRenderer.paint(
            painter,
            QRectF(self.rect()),
            self._snapshot,
            pulse_progress=pulse_progress,
            pulse_kind=self._pulse_kind,
        )
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._interactive:
            event.ignore()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.context_requested.emit(event.globalPosition().toPoint())
            event.accept()
        else:
            event.ignore()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._reanchor()
        progress = self._pulse_progress()
        if progress is not None:
            if progress < 1.0 and not self._closing:
                self._pulse_timer.start()
            else:
                self._clear_pulse(emit_finished=False)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        self._pulse_timer.stop()
        super().hideEvent(event)

    def shutdown(self) -> None:
        """Idempotently stop animation and detach display-change callbacks."""

        if self._closing:
            return
        self._closing = True
        self._clear_pulse(emit_finished=False)
        self._disconnect_anchor_screen()
        self.hide()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)


__all__ = [
    "DEFAULT_CORNER_SIZE",
    "FOCUS_MINUTES_PER_GROWTH_UNIT",
    "GROWTH_UNITS_PER_LEVEL",
    "MoonCornerSnapshot",
    "MoonCornerWidget",
    "MoonRenderer",
    "growth_from_history",
]
