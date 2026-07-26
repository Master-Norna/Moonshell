from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
import ctypes
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from pet.logging_setup import configure_logging, install_exception_hook
from pet.paths import migrate_legacy_data
from pet.pet_window import PixelPetWindow
from pet.settings import Settings
from pet.version import APP_NAME, APP_VERSION


def _instance_name() -> str:
    user_key = str(Path.home()).casefold().encode("utf-8", errors="replace")
    if os.environ.get("MOONSHELL_SMOKE_TEST") == "1":
        # Release smoke runs use an isolated namespace so two test processes
        # can exercise the real single-instance activation path without ever
        # colliding with a user's running copy.
        namespace = os.environ.get("MOONSHELL_SMOKE_NAMESPACE") or str(os.getpid())
        user_key += b":smoke:" + namespace.encode("utf-8", errors="replace")
    return f"MoonShellSpirit-{hashlib.sha256(user_key).hexdigest()[:12]}"


def _smoke_exit_delay_ms() -> int:
    """Return a bounded release-smoke timeout, disabled for normal launches."""

    if os.environ.get("MOONSHELL_SMOKE_TEST") != "1":
        return 0
    try:
        delay = int(os.environ.get("MOONSHELL_SMOKE_EXIT_MS", "0"))
    except (TypeError, ValueError):
        return 0
    return delay if 100 <= delay <= 10_000 else 0


class SingleInstanceGuard:
    def __init__(self, name: str) -> None:
        self.name = name
        self.server = QLocalServer()
        self._mutex_handle: Optional[int] = None

    def close(self) -> None:
        self.server.close()
        if self._mutex_handle and sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(ctypes.c_void_p(self._mutex_handle))
            self._mutex_handle = None


def _notify_existing(name: str) -> bool:
    for _ in range(4):
        peer = QLocalSocket()
        peer.connectToServer(name)
        if peer.waitForConnected(150):
            peer.write(b"activate")
            peer.waitForBytesWritten(150)
            peer.disconnectFromServer()
            return True
        time.sleep(0.05)
    return False


def _acquire_single_instance(name: str | None = None) -> SingleInstanceGuard | None:
    name = name or _instance_name()
    guard = SingleInstanceGuard(name)

    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(handle)
            _notify_existing(name)
            return None
        guard._mutex_handle = int(handle)
        QLocalServer.removeServer(name)
        if not guard.server.listen(name):
            logging.getLogger(__name__).error(
                "Could not create local activation server %s: %s",
                name,
                guard.server.errorString(),
            )
            guard.close()
            return None
        return guard

    if guard.server.listen(name):
        return guard
    if _notify_existing(name):
        return None
    # A crashed Unix process can leave its local socket behind. Only remove it
    # after connection attempts prove that no live instance is listening.
    QLocalServer.removeServer(name)
    if guard.server.listen(name):
        return guard
    logging.getLogger(__name__).error(
        "Could not create local activation server %s: %s",
        name,
        guard.server.errorString(),
    )
    guard.close()
    return None


def main() -> int:
    # Keep fractional monitor scale factors visible to Qt instead of letting a
    # rounded/OS-scaled window soften the pixel sprite.
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    configure_logging()
    install_exception_hook()

    instance_guard = _acquire_single_instance()
    if instance_guard is None:
        return 0
    instance_server = instance_guard.server

    migrated_files = migrate_legacy_data()
    if migrated_files:
        logging.getLogger(__name__).info(
            "Migrated legacy data: %s",
            ", ".join(str(path) for path in migrated_files),
        )

    settings = Settings.load()
    win = PixelPetWindow(settings=settings, root=Path(__file__).resolve().parent)
    smoke_delay = _smoke_exit_delay_ms()
    if smoke_delay:
        QTimer.singleShot(smoke_delay, app.quit)

    def activate_existing_window() -> None:
        while instance_server.hasPendingConnections():
            socket = instance_server.nextPendingConnection()
            socket.readAll()
            socket.disconnectFromServer()
            socket.deleteLater()
        logging.getLogger(__name__).info("Second instance requested activation")
        win.activate_from_second_instance()

    instance_server.newConnection.connect(activate_existing_window)
    app._instance_guard = instance_guard  # type: ignore[attr-defined]
    app.aboutToQuit.connect(instance_guard.close)
    logging.getLogger(__name__).info("Application event loop started")
    try:
        exit_code = app.exec()
    finally:
        # aboutToQuit covers normal exits; this also protects an exceptional
        # event-loop unwind where Qt never emits it.
        win._shutdown()
        instance_guard.close()
    logging.getLogger(__name__).info("Application event loop stopped with code %s", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
