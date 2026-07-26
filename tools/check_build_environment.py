from __future__ import annotations

import argparse
import importlib.metadata
import re
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "requirements-build.txt"
NAME_SEPARATORS = re.compile(r"[-_.]+")
REQUIREMENT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9_.+!-]*)\s+(.*)$"
)
SHA256 = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})(?:\s|$)")


def normalize_name(name: str) -> str:
    return NAME_SEPARATORS.sub("-", name).casefold()


def parse_lock(path: Path) -> tuple[dict[str, str], list[str]]:
    logical_lines: list[str] = []
    pending = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if pending:
            pending += " " + line
        else:
            pending = line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        logical_lines.append(pending)

    locked: dict[str, str] = {}
    failures: list[str] = []
    for line in logical_lines:
        match = REQUIREMENT.fullmatch(line)
        if match is None:
            failures.append(f"unrecognized or unpinned lock entry: {line}")
            continue
        package, version, options = match.groups()
        normalized = normalize_name(package)
        if normalized in locked:
            failures.append(f"duplicate locked package: {package}")
            continue
        hashes = SHA256.findall(options)
        if not hashes:
            failures.append(f"locked package has no SHA-256 wheel hash: {package}")
            continue
        locked[normalized] = version
    return locked, failures


def installed_packages() -> dict[str, str]:
    result: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            result[normalize_name(name)] = distribution.version
    return result


def check_environment(
    lock_path: Path,
    *,
    installed: Mapping[str, str] | None = None,
) -> list[str]:
    locked, failures = parse_lock(lock_path)
    actual = {
        normalize_name(name): version
        for name, version in (
            installed.items() if installed is not None else installed_packages().items()
        )
    }
    for package, expected_version in sorted(locked.items()):
        actual_version = actual.get(package)
        if actual_version is None:
            failures.append(f"locked package is not installed: {package}")
        elif actual_version != expected_version:
            failures.append(
                f"locked package version mismatch: {package} "
                f"expected {expected_version}, got {actual_version}"
            )
    for package in sorted(actual.keys() - locked.keys()):
        failures.append(f"unexpected package in isolated build environment: {package}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the isolated release environment against its hashed lock."
    )
    parser.add_argument("--lock", type=Path, default=LOCK_FILE)
    args = parser.parse_args()
    failures = check_environment(args.lock)
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    locked, _ = parse_lock(args.lock)
    print(f"build environment matches {len(locked)} hashed package locks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
