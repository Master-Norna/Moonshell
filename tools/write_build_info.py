from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import platform
import shutil
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pet.storage import atomic_write_json  # noqa: E402
from pet.version import APP_NAME, APP_VERSION, PROJECT_URL  # noqa: E402
from tools.check_build_environment import (  # noqa: E402
    check_environment,
    installed_packages,
    parse_lock,
)
from tools.release_inputs import release_input_paths  # noqa: E402
from tools.source_provenance import collect_source_provenance  # noqa: E402

RUNTIME_PACKAGE_NAMES = (
    "PySide6-Essentials",
    "shiboken6",
    "psutil",
)
BUILD_PACKAGE_NAMES = (
    "Pillow",
    "PyInstaller",
    "pyinstaller-hooks-contrib",
)


def _distribution_license(distribution_name: str) -> Path | None:
    distribution = importlib.metadata.distribution(distribution_name)
    for relative in distribution.files or ():
        name = str(relative).replace("\\", "/").lower()
        if (
            name.endswith("/license")
            or name.endswith("/license.txt")
            or name.endswith("/copying.txt")
            or name.endswith("/copying")
        ):
            candidate = Path(distribution.locate_file(relative))
            if candidate.is_file():
                return candidate
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_build_info(output: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime_packages = {
        name: importlib.metadata.version(name)
        for name in RUNTIME_PACKAGE_NAMES
    }
    build_packages = {
        name: importlib.metadata.version(name)
        for name in BUILD_PACKAGE_NAMES
    }
    locked_build_environment, lock_failures = parse_lock(
        ROOT / "requirements-build.txt"
    )
    if lock_failures:
        raise ValueError("; ".join(lock_failures))
    actual_build_environment = installed_packages()
    environment_failures = check_environment(
        ROOT / "requirements-build.txt",
        installed=actual_build_environment,
    )
    if environment_failures:
        raise ValueError("; ".join(environment_failures))
    provenance = collect_source_provenance(
        ROOT,
        f"v{APP_VERSION}",
    )
    release_inputs = release_input_paths(ROOT)
    try:
        from PySide6.QtCore import qVersion

        qt_version = qVersion()
    except Exception:
        qt_version = ""
    atomic_write_json(
        output,
        {
            "schema": 3,
            "application": APP_NAME,
            "version": APP_VERSION,
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "python_openssl": ssl.OPENSSL_VERSION,
            "qt": qt_version,
            "runtime_packages": runtime_packages,
            "build_packages": build_packages,
            "locked_build_environment": locked_build_environment,
            "actual_build_environment": actual_build_environment,
            "packages": {**runtime_packages, **build_packages},
            "build_platform": platform.platform(),
            "source": PROJECT_URL,
            "source_archive": (
                f"{PROJECT_URL}/archive/{provenance['source_commit']}.zip"
                if provenance["source_commit"]
                else ""
            ),
            **provenance,
            "release_input_sha256": {
                str(path.relative_to(ROOT)).replace("\\", "/"): _sha256(path)
                for path in release_inputs
            },
            "packaging": "PyInstaller onedir",
        },
    )

    license_dir = output.parent / "LICENSES"
    license_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(path for path in (ROOT / "LICENSES").rglob("*") if path.is_file()):
        relative = source.relative_to(ROOT / "LICENSES")
        destination = license_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError(f"CPython license not found: {python_license}")
    shutil.copy2(python_license, license_dir / "CPython-LICENSE.txt")

    for distribution_name, output_name in (
        ("psutil", "psutil-LICENSE.txt"),
        ("PyInstaller", "PyInstaller-LICENSE.txt"),
    ):
        source = _distribution_license(distribution_name)
        if source is None:
            raise FileNotFoundError(
                f"License file not found in {distribution_name} distribution"
            )
        shutil.copy2(source, license_dir / output_name)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record exact release dependencies and collect licenses."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "MoonShell" / "BUILD_INFO.json",
    )
    args = parser.parse_args()
    path = write_build_info(args.output)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
