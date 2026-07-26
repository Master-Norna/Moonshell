from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pet.version import APP_NAME, APP_VERSION  # noqa: E402
from tools.build_icon import ICON_SIZES  # noqa: E402
from tools.check_build_environment import normalize_name, parse_lock  # noqa: E402
from tools.release_inputs import release_input_paths  # noqa: E402
from tools.source_provenance import (  # noqa: E402
    COMMIT,
    collect_source_provenance,
)


def _check_source_release_inputs() -> list[str]:
    failures: list[str] = []
    required = (
        ROOT / "MoonShell.spec",
        ROOT / ".python-version",
        ROOT / "assets" / "branding" / "moonshell.ico",
        ROOT / "packaging" / "windows_version_info.txt",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "docs" / "preview.png",
        ROOT / "LICENSES" / "GPL-3.0.txt",
        ROOT / "LICENSES" / "LGPL-3.0.txt",
        ROOT / "LICENSES" / "Apache-2.0.txt",
        ROOT / "LICENSES" / "OpenSSL-3.0.21-SOURCE.txt",
        ROOT / "LICENSES" / "Qt-6.11.1-Third-Party-Notices.txt",
        ROOT / "LICENSES" / "Qt-6.11.1-SBOM.spdx.json",
        ROOT / "LICENSES" / "Qt-6.11.1-SOURCE.txt",
    )
    for path in required:
        if not path.is_file():
            failures.append(f"missing release input: {path}")
    for path in release_input_paths(ROOT):
        if not path.is_file() and f"missing release input: {path}" not in failures:
            failures.append(f"missing release input: {path}")

    qt_notice_path = ROOT / "LICENSES" / "Qt-6.11.1-Third-Party-Notices.txt"
    qt_sbom_path = ROOT / "LICENSES" / "Qt-6.11.1-SBOM.spdx.json"
    qt_source_path = ROOT / "LICENSES" / "Qt-6.11.1-SOURCE.txt"
    if qt_notice_path.is_file():
        notice = qt_notice_path.read_text(encoding="utf-8")
        if "Mesa llvmpipe software OpenGL renderer" not in notice:
            failures.append("Qt NOTICE does not cover the opengl32sw.dll fallback")
    if qt_sbom_path.is_file():
        try:
            sbom = json.loads(qt_sbom_path.read_text(encoding="utf-8"))
            package_names = {
                package.get("name")
                for package in sbom.get("packages", [])
                if isinstance(package, dict)
            }
            if "Mesa llvmpipe software OpenGL renderer" not in package_names:
                failures.append("Qt SPDX inventory does not cover opengl32sw.dll")
        except (OSError, ValueError) as exc:
            failures.append(f"invalid Qt SPDX inventory: {exc}")
    if qt_source_path.is_file():
        source_record = qt_source_path.read_text(encoding="utf-8")
        for label, digest in (
            (
                "Qt Translations",
                "37c02c81206594c7bb4edca85ac93e8e55a9836b70c960fde6cb0f8623ec5677",
            ),
            (
                "Qt for Python / Shiboken",
                "6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2",
            ),
            (
                "LLVMPipe",
                "4a7d90f91fdecb5df7b426bc2d05974b8d7ffa450af2d1f93f3eca05800718da",
            ),
        ):
            if digest not in source_record:
                failures.append(f"Qt source record is missing {label} provenance")

    version_text = (ROOT / "packaging" / "windows_version_info.txt").read_text(
        encoding="utf-8"
    )
    if version_text.count(f'"{APP_VERSION}"') < 2:
        failures.append("Windows string metadata does not match APP_VERSION")
    numeric = tuple(int(part) for part in APP_VERSION.split(".")) + (0,)
    expected_tuple = f"({', '.join(str(part) for part in numeric)})"
    if version_text.count(expected_tuple) < 2:
        failures.append("Windows numeric metadata does not match APP_VERSION")

    with Image.open(ROOT / "assets" / "branding" / "moonshell.ico") as icon:
        sizes = set(icon.ico.sizes())  # type: ignore[attr-defined]
    expected_sizes = {(size, size) for size in ICON_SIZES}
    if sizes != expected_sizes:
        failures.append(
            f"ICO size mismatch: expected={sorted(expected_sizes)}, actual={sorted(sizes)}"
        )

    requirements_path = ROOT / "requirements-build.txt"
    build_requirements = requirements_path.read_text(encoding="utf-8")
    for package in (
        "PySide6-Essentials==",
        "psutil==",
        "Pillow==",
        "PyInstaller==",
        "pyinstaller-hooks-contrib==",
    ):
        if package not in build_requirements:
            failures.append(f"release dependency is not pinned: {package[:-2]}")
    locked, lock_failures = parse_lock(requirements_path)
    failures.extend(lock_failures)
    expected_locks = {
        "altgraph",
        "packaging",
        "pefile",
        "pillow",
        "pip",
        "psutil",
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "pyside6-essentials",
        "pywin32-ctypes",
        "setuptools",
        "shiboken6",
    }
    if set(locked) != expected_locks:
        failures.append(
            "hashed build lock package set mismatch: "
            f"expected={sorted(expected_locks)}, actual={sorted(locked)}"
        )
    return failures


def _pe_machine(executable: Path) -> int:
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("missing MZ header")
        stream.seek(0x3C)
        pe_offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(pe_offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("missing PE header")
        return struct.unpack("<H", stream.read(2))[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_recorded_build_environment(
    build_info: dict[str, object],
    expected_lock: dict[str, str],
) -> list[str]:
    failures: list[str] = []
    if build_info.get("locked_build_environment") != expected_lock:
        failures.append("BUILD_INFO does not record the complete hashed build lock")
    if build_info.get("actual_build_environment") != expected_lock:
        failures.append(
            "BUILD_INFO actual build environment does not match the hashed lock"
        )

    runtime_names = (
        "PySide6-Essentials",
        "shiboken6",
        "psutil",
    )
    build_names = (
        "Pillow",
        "PyInstaller",
        "pyinstaller-hooks-contrib",
    )
    expected_runtime = {
        name: expected_lock.get(normalize_name(name))
        for name in runtime_names
    }
    expected_build = {
        name: expected_lock.get(normalize_name(name))
        for name in build_names
    }
    if build_info.get("runtime_packages") != expected_runtime:
        failures.append("BUILD_INFO runtime package versions do not match the lock")
    if build_info.get("build_packages") != expected_build:
        failures.append("BUILD_INFO build package versions do not match the lock")
    if build_info.get("packages") != {**expected_runtime, **expected_build}:
        failures.append("BUILD_INFO package summary does not match the lock")
    return failures


def _check_release_input_hashes(
    build_info: dict[str, object],
    *,
    root: Path = ROOT,
) -> list[str]:
    failures: list[str] = []
    paths = release_input_paths(root)
    current_inputs = {
        path.relative_to(root).as_posix(): path
        for path in paths
    }
    recorded = build_info.get("release_input_sha256")
    if not isinstance(recorded, dict):
        return ["BUILD_INFO release input hashes are missing"]

    expected_keys = set(current_inputs)
    actual_keys = {
        key
        for key in recorded
        if isinstance(key, str)
    }
    if actual_keys != expected_keys or len(actual_keys) != len(recorded):
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        failures.append(
            "BUILD_INFO release input hash set mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )

    hash_pattern = re.compile(r"^[0-9a-f]{64}$")
    for relative in sorted(expected_keys & actual_keys):
        expected_hash = recorded.get(relative)
        path = current_inputs[relative]
        if (
            not isinstance(expected_hash, str)
            or hash_pattern.fullmatch(expected_hash) is None
            or not path.is_file()
            or _sha256(path) != expected_hash
        ):
            failures.append(f"release input changed after build: {relative}")
    return failures


def _check_stable_source_record(
    build_info: dict[str, object],
    *,
    root: Path = ROOT,
    current_provenance: dict[str, object] | None = None,
) -> list[str]:
    failures: list[str] = []
    expected_tag = f"v{APP_VERSION}"
    if build_info.get("source_dirty") is not False:
        failures.append("stable release source is dirty")
    if build_info.get("source_git_verified") is not True:
        failures.append("stable release Git provenance is not verified")
    if build_info.get("source_on_main") is not True:
        failures.append("stable release source is not contained in origin/main")
    if build_info.get("release_eligible") is not True:
        failures.append("BUILD_INFO does not mark this build release-eligible")
    commit = build_info.get("source_commit")
    if not isinstance(commit, str) or COMMIT.fullmatch(commit) is None:
        failures.append("stable release source commit is invalid")
    if build_info.get("source_tag") != expected_tag:
        failures.append(
            f"stable release tag must exactly match application version {expected_tag}"
        )
    tag_target = build_info.get("source_tag_target")
    if (
        not isinstance(tag_target, str)
        or COMMIT.fullmatch(tag_target) is None
        or not isinstance(commit, str)
        or tag_target.casefold() != commit.casefold()
    ):
        failures.append("stable release version tag does not resolve to source HEAD")
    main_target = build_info.get("source_main_target")
    if not isinstance(main_target, str) or COMMIT.fullmatch(main_target) is None:
        failures.append("stable release origin/main target is invalid")

    current = (
        collect_source_provenance(root, expected_tag)
        if current_provenance is None
        else current_provenance
    )
    if current.get("release_eligible") is not True:
        failures.append("current checkout is not a clean, verified version tag")
    if current.get("source_on_main") is not True:
        failures.append("current checkout is not contained in origin/main")
    if commit != current.get("source_commit"):
        failures.append("BUILD_INFO source commit does not match current HEAD")
    if build_info.get("source_tag") != current.get("source_tag"):
        failures.append("BUILD_INFO source tag does not match current HEAD")
    if tag_target != current.get("source_tag_target"):
        failures.append("BUILD_INFO tag target does not match current HEAD")
    return failures


def _check_built_release(
    executable: Path,
    *,
    require_clean_source: bool = False,
) -> list[str]:
    failures: list[str] = []
    executable = executable.resolve()
    distribution_dir = executable.parent
    if not executable.is_file():
        return [f"built executable not found: {executable}"]
    if executable.stat().st_size < 1024 * 1024:
        failures.append("built executable is unexpectedly small")
    try:
        if _pe_machine(executable) != 0x8664:
            failures.append("built executable is not AMD64")
    except (OSError, ValueError, struct.error) as exc:
        failures.append(f"invalid Windows executable: {exc}")

    required_patterns = (
        "qwindows.dll",
        "assets/moonshell/idle.png",
        "assets/branding/moonshell.ico",
        "BUILD_INFO.json",
        "LICENSES/GPL-3.0.txt",
        "LICENSES/LGPL-3.0.txt",
        "LICENSES/Apache-2.0.txt",
        "LICENSES/CPython-LICENSE.txt",
        "LICENSES/OpenSSL-3.0.21-SOURCE.txt",
        "LICENSES/psutil-LICENSE.txt",
        "LICENSES/PyInstaller-LICENSE.txt",
        "LICENSES/Qt-6.11.1-Third-Party-Notices.txt",
        "LICENSES/Qt-6.11.1-SBOM.spdx.json",
        "LICENSES/Qt-6.11.1-SOURCE.txt",
        "opengl32sw.dll",
    )
    normalized_files = {
        path.relative_to(distribution_dir).as_posix().lower()
        for path in distribution_dir.rglob("*")
        if path.is_file()
    }
    for pattern in required_patterns:
        pattern = pattern.lower()
        if not any(path.endswith(pattern) for path in normalized_files):
            failures.append(f"built release is missing: {pattern}")

    forbidden_fragments = (
        "libcrypto-3-x64.dll",
        "libssl-3-x64.dll",
        "pyside6_addons",
        "qopensslbackend.dll",
        "qoffscreen.dll",
        "qminimal.dll",
        "qsvg",
        "qt6webengine",
        "qt6multimedia",
        "qt6qml",
        "qt6svg",
    )
    for fragment in forbidden_fragments:
        matches = sorted(path for path in normalized_files if fragment in path)
        if matches:
            failures.append(
                f"unexpected heavyweight Qt component {fragment}: {matches[0]}"
            )

    build_info_path = distribution_dir / "BUILD_INFO.json"
    if build_info_path.is_file():
        try:
            build_info = json.loads(build_info_path.read_text(encoding="utf-8"))
            if build_info.get("application") != APP_NAME:
                failures.append("BUILD_INFO application name mismatch")
            if build_info.get("version") != APP_VERSION:
                failures.append("BUILD_INFO version mismatch")
            if build_info.get("schema") != 3:
                failures.append("BUILD_INFO schema is not 3")
            if str(build_info.get("architecture", "")).upper() not in {
                "AMD64",
                "X86_64",
            }:
                failures.append("BUILD_INFO architecture is not AMD64")
            expected_python = (
                ROOT / ".python-version"
            ).read_text(encoding="utf-8").strip()
            if build_info.get("python") != expected_python:
                failures.append(
                    "BUILD_INFO Python version does not match .python-version"
                )
            if build_info.get("qt") != build_info.get(
                "runtime_packages", {}
            ).get("PySide6-Essentials"):
                failures.append("BUILD_INFO Qt and PySide6 versions do not match")
            if not str(build_info.get("python_openssl", "")).startswith(
                "OpenSSL 3.0.21 "
            ):
                failures.append("BUILD_INFO OpenSSL version is not 3.0.21")
            expected_lock, lock_failures = parse_lock(
                ROOT / "requirements-build.txt"
            )
            if lock_failures:
                failures.extend(lock_failures)
            else:
                failures.extend(
                    _check_recorded_build_environment(build_info, expected_lock)
                )
            failures.extend(_check_release_input_hashes(build_info))
            if require_clean_source:
                failures.extend(_check_stable_source_record(build_info))
        except (OSError, ValueError) as exc:
            failures.append(f"invalid BUILD_INFO.json: {exc}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate MoonShell release inputs.")
    parser.add_argument("--exe", type=Path, help="Validate a built onedir executable.")
    parser.add_argument(
        "--require-clean-source",
        action="store_true",
        help="Require BUILD_INFO to identify a clean, traceable source commit.",
    )
    args = parser.parse_args()

    failures = _check_source_release_inputs()
    if args.exe is not None:
        failures.extend(
            _check_built_release(
                args.exe,
                require_clean_source=args.require_clean_source,
            )
        )
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print(f"release checks passed for {APP_NAME} {APP_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
