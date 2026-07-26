from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ROOT_INPUTS = (
    ".github/workflows/release.yml",
    ".github/workflows/tests.yml",
    ".python-version",
    "LICENSE",
    "MoonShell.spec",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/daily-card-preview.png",
    "docs/preview.png",
    "build-release.ps1",
    "main.py",
    "requirements-build.txt",
    "requirements.txt",
    "start.cmd",
    "start.ps1",
)

TREE_INPUTS = (
    ("assets/_masters", None),
    ("assets/branding", None),
    ("assets/moonshell", None),
    ("LICENSES", None),
    ("packaging", None),
    ("pet", {".py"}),
    ("tests", {".py"}),
    ("tools", {".py"}),
    (".github/workflows", {".yml", ".yaml"}),
)


def release_input_paths(root: Path = ROOT) -> tuple[Path, ...]:
    """Return every source, resource, compliance, and release-pipeline input."""
    paths = {root / relative for relative in ROOT_INPUTS}
    for relative, suffixes in TREE_INPUTS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            if suffixes is not None and path.suffix.casefold() not in suffixes:
                continue
            paths.add(path)
    return tuple(
        sorted(
            paths,
            key=lambda path: path.relative_to(root).as_posix().casefold(),
        )
    )
