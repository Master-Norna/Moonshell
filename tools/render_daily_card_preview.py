"""Regenerate the tracked daily-card preview through the real card renderer."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pet.pet_window as pet_window
from pet.moon_phase import MoonPhase
from pet.settings import Settings


OUT = ROOT / "docs" / "daily-card-preview.png"


class _SilentMonitor(QObject):
    telemetry = Signal(object)

    def __init__(self, parent=None, *, active: bool = True) -> None:
        super().__init__(parent)
        self.active = active

    def set_active(self, active: bool) -> None:
        self.active = active

    def shutdown(self) -> None:
        self.active = False


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    for font_path in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/Deng.ttf"),
    ):
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
            break
    phase = MoonPhase(
        fraction=0.5,
        age_days=14.8,
        illumination=1.0,
        index=4,
        name="满月",
        emoji="🌕",
        event_key="preview-full-moon",
        event_distance_days=0.0,
    )
    with tempfile.TemporaryDirectory() as temporary:
        state_path = Path(temporary) / "preview-state.json"
        with (
            patch.object(pet_window, "SystemMonitor", _SilentMonitor),
            patch.object(
                QSystemTrayIcon,
                "isSystemTrayAvailable",
                return_value=True,
            ),
        ):
            settings = Settings()
            settings.save = lambda *args, **kwargs: None  # type: ignore[method-assign]
            window = pet_window.SpritePetWindow(
                settings,
                ROOT,
                state_path=state_path,
            )
            try:
                today = date.today()
                window._state.first_seen_date = (today - timedelta(days=24)).isoformat()
                window._state.moon_tokens = 15
                window._state.focus_sessions_completed = 9
                window._state.focus_minutes_completed = 275
                window._state.focus_today_date = today.isoformat()
                window._state.focus_today_minutes = 50
                image = window._build_daily_card(phase)
                if not image.save(str(OUT), "PNG"):
                    raise OSError(f"could not write {OUT}")
            finally:
                window._shutdown()
                window.close()
                window.deleteLater()
                app.processEvents()
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
