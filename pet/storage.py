from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object, rejecting roots that cannot be merged safely."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace a small JSON file without exposing a partial write."""
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
