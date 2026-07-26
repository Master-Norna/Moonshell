from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _hidden_startup_info() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def _wait_for_log_marker(
    process: subprocess.Popen[bytes],
    log_path: Path,
    marker: str,
    deadline: float,
) -> None:
    while time.monotonic() < deadline:
        if log_path.is_file():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if marker in text:
                return
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"packaged application exited with code {return_code} "
                f"before logging: {marker}"
            )
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for release smoke log marker: {marker}")


def smoke_release(executable: Path, timeout: float = 30.0) -> None:
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"release executable not found: {executable}")

    with tempfile.TemporaryDirectory(prefix="moonshell-release-smoke-") as temp:
        data_dir = Path(temp)
        environment = os.environ.copy()
        environment.pop("QT_QPA_PLATFORM", None)
        environment.update(
            {
                "MOONSHELL_SMOKE_TEST": "1",
                "MOONSHELL_SMOKE_EXIT_MS": "9000",
                "MOONSHELL_SMOKE_NAMESPACE": uuid.uuid4().hex,
                "MOONSHELL_DATA_DIR": str(data_dir),
            }
        )
        log_path = data_dir / "moonshell.log"
        deadline = time.monotonic() + timeout
        primary = subprocess.Popen(
            [str(executable)],
            cwd=executable.parent,
            env=environment,
            startupinfo=_hidden_startup_info(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_log_marker(
                primary,
                log_path,
                "Application event loop started",
                deadline,
            )

            second_started = time.monotonic()
            secondary = subprocess.run(
                [str(executable)],
                cwd=executable.parent,
                env=environment,
                startupinfo=_hidden_startup_info(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, min(10.0, deadline - time.monotonic())),
                check=False,
            )
            if secondary.returncode != 0:
                raise RuntimeError(
                    f"second packaged instance exited with code {secondary.returncode}"
                )
            if time.monotonic() - second_started > 5.0:
                raise RuntimeError("second packaged instance did not exit promptly")
            if primary.poll() is not None:
                raise RuntimeError("second instance unexpectedly stopped the first instance")

            _wait_for_log_marker(
                primary,
                log_path,
                "Second instance requested activation",
                deadline,
            )
            primary_return_code = primary.wait(
                timeout=max(1.0, deadline - time.monotonic())
            )
            if primary_return_code != 0:
                raise RuntimeError(
                    f"packaged application exited with code {primary_return_code}"
                )
        finally:
            if primary.poll() is None:
                primary.terminate()
                try:
                    primary.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    primary.kill()
                    primary.wait(timeout=3.0)

        if not log_path.is_file():
            raise RuntimeError("packaged application did not create a diagnostic log")
        log_text = log_path.read_text(encoding="utf-8")
        for marker in (
            "Application event loop started",
            "Second instance requested activation",
            "Application event loop stopped with code 0",
        ):
            if marker not in log_text:
                raise RuntimeError(f"release smoke log is missing: {marker}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch the frozen app with qwindows and isolated local data."
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=ROOT / "dist" / "MoonShell" / "MoonShell.exe",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    smoke_release(args.exe, args.timeout)
    print(f"native release smoke test passed: {args.exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
