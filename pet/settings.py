from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .paths import DATA_DIR
from .storage import atomic_write_json, read_json_object

CONFIG_DIR = DATA_DIR
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
    system_awareness: bool = True
    clipboard_reactions: bool = False

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Settings":
        try:
            if path.exists():
                data = read_json_object(path)
                # migrate old wander/quiet_mode toggles into the merged activity dial
                if "activity" not in data:
                    legacy_low = data.get("quiet_mode") is True or data.get("wander") is False
                    data["activity"] = "low" if legacy_low else "high"
                known = asdict(cls())
                # tolerate (and drop) keys from older versions, e.g. monitor_path
                merged = {**known, **{k: v for k, v in data.items() if k in known}}
                if (
                    not isinstance(merged["size_mode"], str)
                    or merged["size_mode"] not in {"small", "standard"}
                ):
                    merged["size_mode"] = "small"
                if (
                    not isinstance(merged["activity"], str)
                    or merged["activity"] not in {"high", "low"}
                ):
                    merged["activity"] = "high"
                if merged["x"] is not None and type(merged["x"]) is not int:
                    merged["x"] = None
                if not isinstance(merged["screen_name"], (str, type(None))):
                    merged["screen_name"] = None
                ratio = merged["x_ratio"]
                try:
                    ratio_value = (
                        float(ratio)
                        if not isinstance(ratio, bool)
                        and isinstance(ratio, (int, float))
                        else None
                    )
                except (OverflowError, TypeError, ValueError):
                    ratio_value = None
                if (
                    ratio_value is None
                    or not math.isfinite(ratio_value)
                    or not 0.0 <= ratio_value <= 1.0
                ):
                    merged["x_ratio"] = None
                else:
                    merged["x_ratio"] = ratio_value
                for key in (
                    "enabled",
                    "always_on_top",
                    "system_awareness",
                    "clipboard_reactions",
                ):
                    if not isinstance(merged[key], bool):
                        merged[key] = known[key]
                return cls(**merged)
        except Exception as exc:
            logger.warning("Could not load settings from %s: %s", path, exc)
        return cls()

    def save(self, path: Path = CONFIG_PATH) -> bool:
        atomic_write_json(path, asdict(self))
        return True
