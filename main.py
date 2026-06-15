from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
import ctypes
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from pet.pet_window import PixelPetWindow
from pet.settings import Settings


def _instance_name() -> str:
    user_key = str(Path.home()).casefold().encode("utf-8", errors="replace")
    return f"MoonShellSpirit-{hashlib.sha256(user_key).hexdigest()[:12]}"


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


def _notify_existing(name: str) -> None:
    for _ in range(4):
        peer = QLocalSocket()
        peer.connectToServer(name)
        if peer.waitForConnected(150):
            peer.write(b"activate")
            peer.waitForBytesWritten(150)
            peer.disconnectFromServer()
            return
        time.sleep(0.05)


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
            guard.close()
            return None
        return guard

    if guard.server.listen(name):
        return guard
    _notify_existing(name)
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
    app.setApplicationName("MoonShell Spirit v14 Stage")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    instance_guard = _acquire_single_instance()
    if instance_guard is None:
        return 0
    instance_server = instance_guard.server

    settings = Settings.load()
    win = PixelPetWindow(settings=settings, root=Path(__file__).resolve().parent)

    def activate_existing_window() -> None:
        while instance_server.hasPendingConnections():
            socket = instance_server.nextPendingConnection()
            socket.readAll()
            socket.disconnectFromServer()
            socket.deleteLater()
        win.activate_from_second_instance()

    instance_server.newConnection.connect(activate_existing_window)
    app._instance_guard = instance_guard  # type: ignore[attr-defined]
    app.aboutToQuit.connect(instance_guard.close)
    win.show()
    logging.getLogger(__name__).info("Application event loop started")
    exit_code = app.exec()
    logging.getLogger(__name__).info("Application event loop stopped with code %s", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
