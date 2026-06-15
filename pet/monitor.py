from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import psutil
from PySide6.QtCore import QObject, QMetaObject, QThread, QTimer, Qt, Signal, Slot

_CREATE_NO_WINDOW = 0x08000000  # keep nvidia-smi from flashing a console
logger = logging.getLogger(__name__)


@dataclass
class Telemetry:
    """A periodic snapshot of the machine's state, fed to the pet's mood brain.

    `gpu` is best-effort (NVIDIA only); None means "couldn't tell", so the brain
    just leans on the other signals instead of pretending the GPU is idle.
    """
    cpu: float
    mem: float
    gpu: Optional[float]
    gpu_memory_pct: Optional[float]
    gpu_sampled: bool
    battery_pct: Optional[float]
    plugged: Optional[bool]
    hour: int
    minute: int
    idle_seconds: float


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def system_idle_seconds() -> float:
    """Seconds since the last system-wide keyboard/mouse input.

    This is how the pet tells whether *you* are around without needing a global
    input hook -- it powers "long idle -> drowsy" and "just came back -> peek".
    Returns 0.0 on non-Windows or if the call fails (treated as "active").
    """
    if sys.platform != "win32":
        return 0.0
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            tick = ctypes.windll.kernel32.GetTickCount64() & 0xFFFFFFFF
            elapsed_ms = (tick - info.dwTime) & 0xFFFFFFFF
            return elapsed_ms / 1000.0
    except Exception:
        pass
    return 0.0


def machine_load(cpu: float, mem: float, gpu: Optional[float]) -> float:
    """Return a stable 0..1 approximation of current machine busyness."""
    parts = [max(0.0, min(100.0, cpu)) / 100.0]
    if gpu is not None:
        parts.append(max(0.0, min(100.0, gpu)) / 100.0)
    mem_part = max(0.0, min(100.0, mem)) / 100.0
    return min(1.0, 0.85 * max(parts) + 0.15 * mem_part)


class _MonitorWorker(QObject):
    telemetry = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.timer: Optional[QTimer] = None
        self._active = True
        try:
            psutil.cpu_percent(interval=None)  # prime the first (always-0) reading
        except Exception:
            pass
        self._ticks = 0
        self._gpu_off = False
        self._gpu_failures = 0
        self._gpu_last: Optional[float] = None
        self._gpu_memory_last: Optional[float] = None

    @Slot()
    def start(self) -> None:
        if self.timer is not None:
            return
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._tick()

    @Slot(bool)
    def set_active(self, active: bool) -> None:
        self._active = active
        if self.timer is None:
            return
        if active and not self.timer.isActive():
            self.timer.start()
            self._tick()
        elif not active:
            self.timer.stop()

    @Slot()
    def stop(self) -> None:
        if self.timer is not None:
            self.timer.stop()

    def _read_gpu(self) -> tuple[Optional[float], Optional[float], bool]:
        """Best-effort NVIDIA core and VRAM utilization, polled every ~6s."""
        if self._gpu_off or sys.platform != "win32":
            return None, None, False
        if (
            self._ticks % 3 != 0
            and (self._gpu_last is not None or self._gpu_memory_last is not None)
        ):
            return self._gpu_last, self._gpu_memory_last, False
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=1.5,
                creationflags=_CREATE_NO_WINDOW,
            )
            if out.returncode == 0:
                rows: list[tuple[float, float]] = []
                for line in out.stdout.splitlines():
                    parts = [part.strip() for part in line.split(",")]
                    if len(parts) != 3:
                        continue
                    util, used, total = (float(part) for part in parts)
                    rows.append((util, 100.0 * used / total if total > 0 else 0.0))
                if rows:
                    self._gpu_failures = 0
                    self._gpu_last = max(row[0] for row in rows)
                    self._gpu_memory_last = max(row[1] for row in rows)
                    return self._gpu_last, self._gpu_memory_last, True
        except Exception as exc:
            logger.debug("GPU telemetry read failed: %s", exc)
        self._gpu_failures += 1
        if self._gpu_failures >= 3:
            self._gpu_off = True
            logger.info("GPU telemetry disabled after repeated nvidia-smi failures")
        return self._gpu_last, self._gpu_memory_last, False

    @Slot()
    def _tick(self) -> None:
        if not self._active:
            return
        try:
            cpu = float(psutil.cpu_percent(interval=None))
            mem = float(psutil.virtual_memory().percent)
        except Exception:
            return
        self._ticks += 1

        gpu, gpu_memory_pct, gpu_sampled = self._read_gpu()

        batt_pct: Optional[float] = None
        plugged: Optional[bool] = None
        try:
            b = psutil.sensors_battery()
            if b is not None:
                batt_pct = float(b.percent)
                plugged = bool(b.power_plugged)
        except Exception:
            pass

        lt = time.localtime()
        self.telemetry.emit(Telemetry(
            cpu=cpu, mem=mem, gpu=gpu, gpu_memory_pct=gpu_memory_pct,
            gpu_sampled=gpu_sampled,
            battery_pct=batt_pct, plugged=plugged,
            hour=lt.tm_hour, minute=lt.tm_min,
            idle_seconds=system_idle_seconds(),
        ))


class SystemMonitor(QObject):
    """Runs hardware polling off the GUI thread and forwards snapshots."""

    telemetry = Signal(object)
    _active_changed = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._stopped = False
        self._thread = QThread(self)
        self._thread.setObjectName("system-monitor")
        self._worker = _MonitorWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._worker.telemetry.connect(self.telemetry)
        self._active_changed.connect(
            self._worker.set_active,
            Qt.ConnectionType.QueuedConnection,
        )
        self._thread.start()

    def set_active(self, active: bool) -> None:
        if not self._stopped:
            self._active_changed.emit(active)

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._thread.isRunning():
            QMetaObject.invokeMethod(
                self._worker,
                "stop",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
            self._thread.quit()
            self._thread.wait(2500)
