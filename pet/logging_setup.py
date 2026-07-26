from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from .settings import CONFIG_DIR

LOG_FILENAME = "moonshell.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 2


def configure_logging(config_dir: Path = CONFIG_DIR) -> Optional[Path]:
    """Configure durable logs for both source and windowed/frozen launches.

    A PyInstaller ``--windowed`` process has no console, so relying on stderr
    makes startup and Qt callback failures impossible to diagnose. File logging
    is best effort: a read-only home directory must never prevent the pet from
    starting.
    """

    handlers: list[logging.Handler] = []
    log_path = config_dir / LOG_FILENAME
    file_error: Optional[Exception] = None
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    except Exception as exc:
        file_error = exc
        log_path = None

    if getattr(sys, "stderr", None) is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )
    if file_error is not None:
        logging.getLogger(__name__).warning(
            "Could not create the application log: %s", file_error
        )
    return log_path


def install_exception_hook() -> None:
    """Route uncaught Python/Qt callback exceptions into the durable log."""

    original_hook = sys.excepthook

    def log_uncaught(
        exc_type: Type[BaseException],
        exc_value: BaseException,
        exc_traceback: Optional[TracebackType],
    ) -> None:
        logging.getLogger("moonshell.crash").critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        if getattr(sys, "stderr", None) is not None:
            original_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = log_uncaught


def close_logging() -> None:
    """Close and detach file handles before the user clears local data."""

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        try:
            handler.flush()
        finally:
            handler.close()
    root.addHandler(logging.NullHandler())
