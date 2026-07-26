from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .storage import atomic_write_json, read_json_object

APP_DATA_FOLDER = "MoonShell"
DATA_DIR_ENV = "MOONSHELL_DATA_DIR"
LEGACY_DATA_DIR = Path.home() / ".desktop_pet_mvp"
MIGRATION_MARKER = ".migrated-to-moonshell-v1"
KNOWN_DATA_FILENAMES = (
    "settings.json",
    "settings.json.tmp",
    "state.json",
    "state.json.tmp",
    "moonshell.log",
    "moonshell.log.1",
    "moonshell.log.2",
)


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / APP_DATA_FOLDER
    return Path.home() / ".moonshell"


def _configured_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if not override:
        return _default_data_dir()
    path = Path(override).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


DATA_DIR = _configured_data_dir()
USING_DATA_DIR_OVERRIDE = bool(os.environ.get(DATA_DIR_ENV))
logger = logging.getLogger(__name__)


def migrate_legacy_data() -> list[Path]:
    """Copy the two durable JSON files from the pre-1.0 data directory once."""

    if USING_DATA_DIR_OVERRIDE or DATA_DIR == LEGACY_DATA_DIR:
        return []
    marker = DATA_DIR / MIGRATION_MARKER
    if marker.exists() or not LEGACY_DATA_DIR.is_dir():
        return []

    migrated: list[Path] = []
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("settings.json", "state.json"):
            source = LEGACY_DATA_DIR / name
            destination = DATA_DIR / name
            if not source.is_file():
                continue
            try:
                payload = read_json_object(source)
            except OSError:
                raise
            except ValueError as exc:
                logger.warning("Could not migrate legacy data from %s: %s", source, exc)
                # A malformed legacy file cannot be recovered by retrying on
                # every launch. Keep it untouched for manual recovery.
                continue
            atomic_write_json(destination, payload)
            migrated.append(destination)
        atomic_write_json(
            marker,
            {
                "source": str(LEGACY_DATA_DIR),
                "destination": str(DATA_DIR),
            },
        )
    except OSError as exc:
        # Settings/state loaders already degrade safely. Migration is best
        # effort so a locked or read-only legacy directory cannot block launch.
        logger.warning("Legacy data migration will be retried: %s", exc)
        return migrated
    return migrated


def clear_known_local_data(*, include_legacy: bool = True) -> list[tuple[Path, OSError]]:
    """Delete only MoonShell-owned files and leave unrelated files untouched."""

    failures: list[tuple[Path, OSError]] = []
    directories = [DATA_DIR]
    if include_legacy and LEGACY_DATA_DIR != DATA_DIR:
        directories.append(LEGACY_DATA_DIR)

    for directory in directories:
        for name in KNOWN_DATA_FILENAMES:
            path = directory / name
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append((path, exc))
        try:
            directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Keep non-empty directories: they can contain files from the user
            # or from a future version that this release must not guess about.
            pass

    legacy_memory_remains = include_legacy and any(
        (LEGACY_DATA_DIR / name).exists()
        for name in ("settings.json", "state.json")
    )
    marker_paths = [DATA_DIR / MIGRATION_MARKER]
    if LEGACY_DATA_DIR != DATA_DIR:
        marker_paths.append(LEGACY_DATA_DIR / MIGRATION_MARKER)
    if legacy_memory_remains:
        # Do not let an occupied/read-only legacy file resurrect itself on the
        # next launch. This tiny tombstone intentionally survives a partial
        # clear and contains no companion data.
        try:
            atomic_write_json(
                DATA_DIR / MIGRATION_MARKER,
                {"legacy_migration_blocked_after_clear": True},
            )
        except OSError as exc:
            failures.append((DATA_DIR / MIGRATION_MARKER, exc))
    else:
        for marker in marker_paths:
            try:
                marker.unlink(missing_ok=True)
            except OSError as exc:
                failures.append((marker, exc))
    return failures
