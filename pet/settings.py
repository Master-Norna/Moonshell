from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path.home() / ".desktop_pet_mvp"
CONFIG_PATH = CONFIG_DIR / "settings.json"
logger = logging.getLogger(__name__)


@dataclass
class Settings:
    enabled: bool = True
    always_on_top: bool = True
    size_mode: str = "small"  # small | standard
    x: Optional[int] = None
    screen_name: Optional[str] = None
    x_ratio: Optional[float] = None
    # 活动强度：high = 自由散步 + 会说话 + 更活泼；low = 待在原地 + 安静 + 更沉静
    # （合并了旧版的「自由活动」开关和「安静模式」）
    activity: str = "high"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Settings":
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("settings root must be an object")
                # migrate old wander/quiet_mode toggles into the merged activity dial
                if "activity" not in data:
                    legacy_low = data.get("quiet_mode") is True or data.get("wander") is False
                    data["activity"] = "low" if legacy_low else "high"
                known = asdict(cls())
                # tolerate (and drop) keys from older versions, e.g. monitor_path
                merged = {**known, **{k: v for k, v in data.items() if k in known}}
                if merged["size_mode"] not in {"small", "standard"}:
                    merged["size_mode"] = "small"
                if merged["activity"] not in {"high", "low"}:
                    merged["activity"] = "high"
                if not isinstance(merged["x"], (int, type(None))):
                    merged["x"] = None
                if not isinstance(merged["screen_name"], (str, type(None))):
                    merged["screen_name"] = None
                ratio = merged["x_ratio"]
                if not isinstance(ratio, (int, float)) or not 0.0 <= float(ratio) <= 1.0:
                    merged["x_ratio"] = None
                else:
                    merged["x_ratio"] = float(ratio)
                for key in ("enabled", "always_on_top"):
                    if not isinstance(merged[key], bool):
                        merged[key] = known[key]
                return cls(**merged)
        except Exception as exc:
            logger.warning("Could not load settings from %s: %s", path, exc)
        return cls()

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        try:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
