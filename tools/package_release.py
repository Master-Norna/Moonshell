from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pet.version import APP_VERSION  # noqa: E402


RELEASE_DOCUMENTS = (
    ("README.md", "README.md"),
    ("LICENSE", "LICENSE"),
    ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
    ("docs/VISUAL_LANGUAGE.md", "docs/VISUAL_LANGUAGE.md"),
    ("docs/runtime-preview-v2.png", "docs/runtime-preview-v2.png"),
    ("docs/v2_character_anchor.png", "docs/v2_character_anchor.png"),
    ("assets/_masters/README.md", "assets/_masters/README.md"),
    ("assets/_masters/idle.png", "assets/_masters/idle.png"),
    ("assets/moonshell/idle.png", "assets/moonshell/idle.png"),
)
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
QA_NOTICE = (
    "This is an unsigned QA build. It is not intended for public release.\n"
    "Verify BUILD_INFO.json for source provenance before testing.\n"
)
UNSIGNED_RELEASE_NOTICE = (
    "This public MoonShell maintenance release is not Authenticode-signed.\n"
    "Download it only from the project's official GitHub Releases page and "
    "verify the published SHA256SUMS.txt before running it.\n"
    "SHA-256 verifies file consistency; it does not authenticate the publisher.\n"
    "Windows may show an Unknown publisher or SmartScreen warning.\n"
)


def _release_files(executable: Path) -> Iterable[tuple[Path, str]]:
    folder = f"MoonShell-{APP_VERSION}"
    distribution_dir = executable.parent
    for path in sorted(distribution_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(distribution_dir).as_posix()
            yield path, f"{folder}/{relative}"
    for source_name, archive_name in RELEASE_DOCUMENTS:
        path = ROOT / source_name
        if not path.is_file():
            raise FileNotFoundError(f"Missing release document: {path}")
        yield path, f"{folder}/{archive_name}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_member(bundle: zipfile.ZipFile, source: Path, archive_name: str) -> None:
    info = zipfile.ZipInfo(archive_name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100644 & 0xFFFF) << 16
    with source.open("rb") as input_stream, bundle.open(info, "w") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def _write_text_member(
    bundle: zipfile.ZipFile,
    text: str,
    archive_name: str,
) -> None:
    info = zipfile.ZipInfo(archive_name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100644 & 0xFFFF) << 16
    bundle.writestr(info, text.encode("utf-8"), compresslevel=9)


def _unsigned_release_failures(executable: Path) -> list[str]:
    from tools.check_release import _check_built_release

    return _check_built_release(
        executable,
        require_clean_source=True,
    )


def _validate_release_archive(
    archive: Path,
    *,
    mode: str,
) -> None:
    with zipfile.ZipFile(archive, "r") as bundle:
        corrupt_member = bundle.testzip()
        if corrupt_member is not None:
            raise ValueError(f"Release ZIP contains a corrupt file: {corrupt_member}")
        members = bundle.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)):
            raise ValueError("Release ZIP contains duplicate paths")
        folder = f"MoonShell-{APP_VERSION}"
        for _, archive_name in RELEASE_DOCUMENTS:
            expected = f"{folder}/{archive_name}"
            if expected not in names:
                raise ValueError(f"Release ZIP is missing: {expected}")
        notices = {
            "qa": f"{folder}/UNSIGNED_BUILD.txt",
            "unsigned-release": f"{folder}/UNSIGNED_RELEASE.txt",
        }
        if mode not in notices:
            raise ValueError(f"Unknown release archive mode: {mode}")
        expected_notice = notices[mode]
        if expected_notice not in names:
            raise ValueError(
                f"{mode} ZIP is missing its unsigned notice"
            )
        unexpected_notices = (
            set(notices.values()) - {expected_notice}
        ) & set(names)
        if unexpected_notices:
            raise ValueError(
                f"{mode} ZIP contains the wrong unsigned notice"
            )
        for name in names:
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != folder
            ):
                raise ValueError(f"Unsafe release ZIP path: {name}")


def package_release(
    executable: Path,
    output_dir: Path,
    *,
    qa: bool = True,
    unsigned_release: bool = False,
) -> tuple[Path, Path]:
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"Built executable not found: {executable}")
    if executable.stat().st_size < 1024 * 1024:
        raise ValueError(f"Built executable is unexpectedly small: {executable}")
    if executable.read_bytes()[:2] != b"MZ":
        raise ValueError(f"Built file is not a Windows executable: {executable}")
    if not any(executable.parent.rglob("qwindows.dll")):
        raise ValueError("Qt's Windows platform plugin is missing from the build")
    if not any(executable.parent.rglob("assets/moonshell/idle.png")):
        raise ValueError("MoonShell sprite assets are missing from the build")
    if qa == unsigned_release:
        raise ValueError(
            "Select exactly one packaging mode: QA or unsigned release"
        )
    mode = "qa" if qa else "unsigned-release"
    if unsigned_release:
        release_failures = _unsigned_release_failures(executable)
        if release_failures:
            details = "\n".join(f"- {failure}" for failure in release_failures)
            raise ValueError(f"Unsigned release policy failed:\n{details}")

    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-unsigned-qa" if qa else "-portable"
    stale_suffix = "-portable" if qa else "-unsigned-qa"
    archive = output_dir / f"MoonShell-{APP_VERSION}-windows-x64{suffix}.zip"
    stale_variant = output_dir / (
        f"MoonShell-{APP_VERSION}-windows-x64{stale_suffix}.zip"
    )
    temp_archive = archive.with_suffix(archive.suffix + ".tmp")
    checksum_file = output_dir / "SHA256SUMS.txt"
    temp_checksum = checksum_file.with_suffix(checksum_file.suffix + ".tmp")
    archive_backup = archive.with_suffix(archive.suffix + ".bak")
    checksum_backup = checksum_file.with_suffix(checksum_file.suffix + ".bak")
    stale_backup = stale_variant.with_suffix(stale_variant.suffix + ".bak")
    backup_paths = (archive_backup, checksum_backup, stale_backup)
    existing_backups = [path for path in backup_paths if path.exists()]
    if existing_backups:
        names = ", ".join(path.name for path in existing_backups)
        raise ValueError(
            "Incomplete release transaction requires inspection before retrying: "
            f"{names}"
        )

    archive_backed_up = False
    checksum_backed_up = False
    stale_backed_up = False
    archive_committed = False
    checksum_committed = False

    try:
        with zipfile.ZipFile(
            temp_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as bundle:
            for source, archive_name in _release_files(executable):
                _write_member(bundle, source, archive_name)
            _write_text_member(
                bundle,
                UNSIGNED_RELEASE_NOTICE if unsigned_release else QA_NOTICE,
                (
                    f"MoonShell-{APP_VERSION}/UNSIGNED_RELEASE.txt"
                    if unsigned_release
                    else f"MoonShell-{APP_VERSION}/UNSIGNED_BUILD.txt"
                ),
            )
        _validate_release_archive(
            temp_archive,
            mode=mode,
        )
        checksum = _sha256(temp_archive)
        temp_checksum.write_text(
            f"{checksum}  {archive.name}\n",
            encoding="ascii",
            newline="\n",
        )

        if stale_variant.exists():
            os.replace(stale_variant, stale_backup)
            stale_backed_up = True
        if archive.exists():
            os.replace(archive, archive_backup)
            archive_backed_up = True
        if checksum_file.exists():
            os.replace(checksum_file, checksum_backup)
            checksum_backed_up = True

        # Commit the manifest first and the already-validated archive last.
        # A failed archive promotion can therefore be rolled back without ever
        # exposing a new final-named release archive.
        os.replace(temp_checksum, checksum_file)
        checksum_committed = True
        os.replace(temp_archive, archive)
        archive_committed = True
    except Exception as exc:
        rollback_errors: list[str] = []

        def rollback(action: str, operation: Callable[[], None]) -> None:
            try:
                operation()
            except OSError as rollback_error:
                rollback_errors.append(f"{action}: {rollback_error}")

        if archive_committed:
            rollback(
                f"remove new {archive.name}",
                lambda: archive.unlink(missing_ok=True),
            )
        if archive_backed_up:
            rollback(
                f"restore {archive.name}",
                lambda: os.replace(archive_backup, archive),
            )
        if checksum_committed:
            rollback(
                f"remove new {checksum_file.name}",
                lambda: checksum_file.unlink(missing_ok=True),
            )
        if checksum_backed_up:
            rollback(
                f"restore {checksum_file.name}",
                lambda: os.replace(checksum_backup, checksum_file),
            )
        if stale_backed_up:
            rollback(
                f"restore {stale_variant.name}",
                lambda: os.replace(stale_backup, stale_variant),
            )
        rollback(
            f"remove temporary {temp_archive.name}",
            lambda: temp_archive.unlink(missing_ok=True),
        )
        rollback(
            f"remove temporary {temp_checksum.name}",
            lambda: temp_checksum.unlink(missing_ok=True),
        )
        if rollback_errors:
            details = "; ".join(rollback_errors)
            raise RuntimeError(
                f"Release packaging failed and rollback was incomplete: {details}"
            ) from exc
        raise

    for backup in backup_paths:
        try:
            backup.unlink(missing_ok=True)
        except OSError as cleanup_error:
            # The new archive and matching manifest are already committed.
            # Retaining a backup is safer than reporting the release as failed;
            # the next packaging attempt will stop for explicit inspection.
            print(
                f"WARNING: could not remove release backup {backup}: "
                f"{cleanup_error}",
                file=sys.stderr,
            )

    return archive, checksum_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package the verified MoonShell Windows release."
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=ROOT / "dist" / "MoonShell" / "MoonShell.exe",
        help="Path to the PyInstaller executable.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist",
        help="Directory for the zip and SHA-256 manifest.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--qa",
        action="store_true",
        help="Package an unsigned QA build (the safe default).",
    )
    mode.add_argument(
        "--unsigned-release",
        action="store_true",
        help=(
            "Package a clean, version-tagged public maintenance release "
            "without Authenticode signing. The ZIP contains an explicit "
            "unsigned-release notice."
        ),
    )
    args = parser.parse_args()
    try:
        archive, checksum = package_release(
            args.exe,
            args.output_dir,
            qa=not args.unsigned_release,
            unsigned_release=args.unsigned_release,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"release archive: {archive}")
    print(f"checksums: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
