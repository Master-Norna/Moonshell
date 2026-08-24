"""Render the README hero from the same assets and moon painter as the app."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pet.moon_corner import MoonCornerSnapshot, MoonRenderer


OUT = ROOT / "docs" / "runtime-preview-v2.png"
LEGACY_OUT = ROOT / "docs" / "preview.png"


def _font(size: int, *, bold: bool = False) -> QFont:
    families = set(QFontDatabase.families())
    if not families:
        # The offscreen Qt platform can start without discovering Windows
        # fonts. Load one explicitly for deterministic documentation rendering;
        # the font file is not copied into or required by the application.
        for path in (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/Deng.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
        ):
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
                families = set(QFontDatabase.families())
                if families:
                    break
    family = next(
        (
            name
            for name in (
                "Microsoft YaHei UI",
                "Microsoft YaHei",
                "DengXian",
            )
            if name in families
        ),
        "Sans Serif",
    )
    font = QFont(family)
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def render() -> QImage:
    image = QImage(1440, 900, QImage.Format.Format_ARGB32)
    image.fill(QColor("#090a1b"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    background = QLinearGradient(0, 0, 1440, 900)
    background.setColorAt(0.0, QColor("#090a1b"))
    background.setColorAt(0.58, QColor("#151941"))
    background.setColorAt(1.0, QColor("#291d52"))
    painter.fillRect(image.rect(), QBrush(background))

    # Fixed, sparse points keep the scene reproducible and quiet.
    painter.setPen(Qt.PenStyle.NoPen)
    for index in range(34):
        x = 42 + (index * 173 + index * index * 7) % 1350
        y = 40 + (index * 97 + index * index * 11) % 790
        radius = 1 + index % 3
        alpha = 55 + index % 5 * 22
        painter.setBrush(QColor(255, 222, 135, alpha))
        painter.drawEllipse(QPoint(x, y), radius, radius)

    painter.setFont(_font(54, bold=True))
    painter.setPen(QColor("#fff3c7"))
    painter.drawText(76, 112, "一只月灵，一轮会长大的月亮")
    painter.setFont(_font(27))
    painter.setPen(QColor("#cdd2ff"))
    painter.drawText(80, 165, "摸摸、月光与专注，共用同一份本地陪伴记忆")

    # Real active sprite, enlarged at an exact integer multiplier.
    sprite = QImage(str(ROOT / "assets" / "moonshell" / "read.png"))
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    painter.drawImage(QRect(92, 410, 384, 384), sprite)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    bubble = QPainterPath()
    bubble.addRoundedRect(QRectF(335, 325, 330, 108), 28, 28)
    tail = QPainterPath()
    tail.moveTo(390, 424)
    tail.lineTo(350, 467)
    tail.lineTo(438, 425)
    tail.closeSubpath()
    bubble = bubble.united(tail)
    painter.setPen(QPen(QColor(255, 233, 167, 86), 2))
    painter.setBrush(QColor(27, 31, 70, 238))
    painter.drawPath(bubble)
    painter.setFont(_font(27))
    painter.setPen(QColor("#fff6da"))
    painter.drawText(
        QRect(370, 344, 260, 66),
        Qt.AlignmentFlag.AlignCenter,
        "这一段，我陪你。",
    )

    snapshot = MoonCornerSnapshot(
        illumination=0.74,
        phase_index=3,
        moon_tokens=9,
        focus_minutes_completed=100,
        focus_active=True,
        focus_progress=0.64,
    )
    MoonRenderer.paint(painter, QRect(975, 70, 350, 350), snapshot)

    # One restrained causal line connects the character action to the moon;
    # this is deliberately not a second dashboard or progression tree.
    connection = QPainterPath(QPointF(492, 590))
    connection.cubicTo(700, 560, 760, 345, 1005, 286)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(
        QPen(
            QColor(174, 184, 255, 92),
            3,
            Qt.PenStyle.DashLine,
            Qt.PenCapStyle.RoundCap,
        )
    )
    painter.drawPath(connection)
    painter.setPen(Qt.PenStyle.NoPen)
    for point in (QPointF(680, 505), QPointF(835, 407), QPointF(965, 320)):
        painter.setBrush(QColor(255, 225, 147, 190))
        painter.drawEllipse(point, 4.0, 4.0)

    panel = QRectF(665, 610, 650, 150)
    painter.setBrush(QColor(24, 28, 65, 220))
    painter.setPen(QPen(QColor(86, 88, 140, 150), 2))
    painter.drawRoundedRect(panel, 24, 24)
    painter.setFont(_font(29, bold=True))
    painter.setPen(QColor("#fff3c7"))
    painter.drawText(710, 666, "专注进行中 · 月亮同步回应")
    painter.setFont(_font(23))
    painter.setPen(QColor("#989fca"))
    painter.drawText(710, 710, "完成后，成长会写回同一份陪伴手账")

    painter.setFont(_font(18))
    painter.setPen(QColor(143, 150, 189, 190))
    painter.drawText(
        QRect(70, 842, 1300, 28),
        Qt.AlignmentFlag.AlignCenter,
        "MoonShell Spirit · 使用真实 Active 像素帧与角落月亮渲染器绘制",
    )
    painter.end()
    return image


def main() -> int:
    QApplication.instance() or QApplication(sys.argv)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image = render()
    for target in (OUT, LEGACY_OUT):
        if not image.save(str(target), "PNG"):
            raise OSError(f"could not write {target}")
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
