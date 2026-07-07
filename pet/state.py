from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

STATE_DIR = Path.home() / ".desktop_pet_mvp"
STATE_PATH = STATE_DIR / "state.json"
logger = logging.getLogger(__name__)


@dataclass
class PetState:
    """Runtime continuity that survives a restart -- so the pet doesn't reset to a
    blank slate every launch but carries a bit of mood and remembers you.

    This is deliberately separate from Settings (user preferences): it's the pet's
    own memory, not something the user configures.
    """
    last_seen: float = 0.0      # epoch seconds when it last saved
    energy: float = 0.6
    mood: float = 0.6
    sleepiness: float = 0.2

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "PetState":
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    known = asdict(cls())
                    merged = {**known, **{k: v for k, v in data.items() if k in known}}
                    for key in ("last_seen", "energy", "mood", "sleepiness"):
                        if not isinstance(merged[key], (int, float)):
                            merged[key] = known[key]
                    return cls(**{k: float(v) for k, v in merged.items()})
        except Exception as exc:
            logger.warning("Could not load pet state from %s: %s", path, exc)
        return cls()

    def save(self, path: Path = STATE_PATH) -> None:
        self.last_seen = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp_path, path)
        except Exception as exc:
            # Losing a save means the pet "forgets" this session -- keep running,
            # but leave a trace so repeated failures are diagnosable.
            logger.warning("Could not save pet state to %s: %s", path, exc)
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def absence_seconds(self) -> float:
        if self.last_seen <= 0:
            return -1.0  # never seen before (first ever launch)
        return max(0.0, time.time() - self.last_seen)
