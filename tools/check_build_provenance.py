from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOC = ROOT / "build" / "MoonShell" / "COLLECT-00.toc"
FORBIDDEN_BINARY_NAMES = {
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "qopensslbackend.dll",
}
TOC_ENTRY_TYPES = {
    "BINARY",
    "DATA",
    "DEPENDENCY",
    "EXTENSION",
    "PYMODULE",
    "PYMODULE-1",
    "PYSOURCE",
}


def _iter_source_entries(value: object) -> Iterable[tuple[str, Path, str]]:
    if (
        isinstance(value, tuple)
        and len(value) >= 3
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and isinstance(value[2], str)
        and value[2] in TOC_ENTRY_TYPES
        and os.path.isabs(value[1])
    ):
        yield value[0], Path(value[1]).resolve(strict=False), value[2]
        return
    if isinstance(value, (tuple, list)):
        for item in value:
            yield from _iter_source_entries(item)


def _is_within(path: Path, root: Path) -> bool:
    path_text = os.path.normcase(str(path.resolve(strict=False)))
    root_text = os.path.normcase(str(root.resolve(strict=False)))
    try:
        return os.path.commonpath((path_text, root_text)) == root_text
    except ValueError:
        return False


def check_provenance(
    toc_path: Path,
    *,
    allowed_roots: Iterable[Path] | None = None,
) -> list[str]:
    toc_path = toc_path.resolve()
    value = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    roots = tuple(
        path.resolve(strict=False)
        for path in (
            allowed_roots
            if allowed_roots is not None
            else (ROOT, Path(sys.base_prefix))
        )
    )

    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for destination, source, entry_type in _iter_source_entries(value):
        source_key = os.path.normcase(str(source))
        key = (destination.casefold(), source_key)
        if key in seen:
            continue
        seen.add(key)
        if Path(destination.replace("\\", "/")).name.casefold() in FORBIDDEN_BINARY_NAMES:
            failures.append(
                f"forbidden binary collected: {destination} <- {source}"
            )
        if not any(_is_within(source, root) for root in roots):
            failures.append(
                f"external {entry_type} source is not allowlisted: "
                f"{destination} <- {source}"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject host-machine contamination in the final PyInstaller collection."
    )
    parser.add_argument("--toc", type=Path, default=DEFAULT_TOC)
    args = parser.parse_args()
    if not args.toc.is_file():
        print(f"ERROR: PyInstaller collection not found: {args.toc}")
        return 1
    failures = check_provenance(args.toc)
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print("PyInstaller dependency provenance is clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
