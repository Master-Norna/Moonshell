from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

from .paths import DATA_DIR
from .storage import atomic_write_json, read_json_object

STATE_DIR = DATA_DIR
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
    first_seen_date: str = ""
    last_gift_date: str = ""
    moon_tokens: int = 0
    focus_until: float = 0.0
    focus_planned_minutes: int = 0
    focus_sessions_completed: int = 0
    focus_minutes_completed: int = 0
    focus_today_date: str = ""
    focus_today_minutes: int = 0
    last_moon_event_key: str = ""

    MAX_FOCUS_PLANNED_MINUTES = 90
    MAX_FOCUS_SESSIONS = 1_000_000
    MAX_FOCUS_MINUTES = 10_000_000
    MAX_FOCUS_TODAY_MINUTES = 24 * 60

    @staticmethod
    def _number(
        value: object,
        default: float,
        *,
        minimum: float = 0.0,
        maximum: float | None = None,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        try:
            result = float(value)
        except (OverflowError, TypeError, ValueError):
            return default
        if not math.isfinite(result):
            return default
        result = max(minimum, result)
        return min(maximum, result) if maximum is not None else result

    @staticmethod
    def _gift_date(value: object) -> str:
        if not isinstance(value, str) or not value:
            return ""
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return ""

    @staticmethod
    def _integer(
        value: object,
        default: int,
        *,
        minimum: int = 0,
        maximum: int = 1_000_000,
    ) -> int:
        if type(value) is not int:
            return default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _moon_event_key(value: object) -> str:
        """Accept only the compact integer keys produced by moon_phase.py."""
        if not isinstance(value, str) or not value or len(value) > 32:
            return ""
        try:
            return str(int(value))
        except ValueError:
            return ""

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "PetState":
        try:
            if path.exists():
                data = read_json_object(path)
                defaults = cls()
                return cls(
                    last_seen=cls._number(data.get("last_seen"), defaults.last_seen),
                    energy=cls._number(
                        data.get("energy"), defaults.energy, maximum=1.0
                    ),
                    mood=cls._number(data.get("mood"), defaults.mood, maximum=1.0),
                    sleepiness=cls._number(
                        data.get("sleepiness"), defaults.sleepiness, maximum=1.0
                    ),
                    first_seen_date=cls._gift_date(data.get("first_seen_date")),
                    last_gift_date=cls._gift_date(data.get("last_gift_date")),
                    moon_tokens=cls._integer(
                        data.get("moon_tokens"), defaults.moon_tokens
                    ),
                    focus_until=cls._number(
                        data.get("focus_until"), defaults.focus_until
                    ),
                    focus_planned_minutes=cls._integer(
                        data.get("focus_planned_minutes"),
                        defaults.focus_planned_minutes,
                        maximum=cls.MAX_FOCUS_PLANNED_MINUTES,
                    ),
                    focus_sessions_completed=cls._integer(
                        data.get("focus_sessions_completed"),
                        defaults.focus_sessions_completed,
                        maximum=cls.MAX_FOCUS_SESSIONS,
                    ),
                    focus_minutes_completed=cls._integer(
                        data.get("focus_minutes_completed"),
                        defaults.focus_minutes_completed,
                        maximum=cls.MAX_FOCUS_MINUTES,
                    ),
                    focus_today_date=cls._gift_date(
                        data.get("focus_today_date")
                    ),
                    focus_today_minutes=cls._integer(
                        data.get("focus_today_minutes"),
                        defaults.focus_today_minutes,
                        maximum=cls.MAX_FOCUS_TODAY_MINUTES,
                    ),
                    last_moon_event_key=cls._moon_event_key(
                        data.get("last_moon_event_key")
                    ),
                )
        except Exception as exc:
            logger.warning("Could not load pet state from %s: %s", path, exc)
        return cls()

    def save(self, path: Path = STATE_PATH) -> bool:
        self.last_seen = time.time()
        try:
            atomic_write_json(path, asdict(self))
        except Exception as exc:
            # Losing a save means the pet "forgets" this session -- keep running,
            # but leave a trace so repeated failures are diagnosable.
            logger.warning("Could not save pet state to %s: %s", path, exc)
            return False
        return True

    def absence_seconds(self) -> float:
        if self.last_seen <= 0:
            return -1.0  # never seen before (first ever launch)
        return max(0.0, time.time() - self.last_seen)

    def companionship_days(self, today: date | None = None) -> int:
        """Calendar days since the first meeting, without streak pressure."""
        try:
            first_seen = date.fromisoformat(self.first_seen_date)
        except ValueError:
            return 1
        current = today or date.today()
        return max(1, (current - first_seen).days + 1)

    def normalize_focus_today(self, today: date | None = None) -> bool:
        """Move the calendar-scoped focus total onto the current local day.

        A past day starts fresh. If the stored date is in the future, preserve
        the minutes while repairing the date: a temporarily incorrect system
        clock must not erase an otherwise valid completion.
        """
        current = today or date.today()
        current_key = current.isoformat()
        if self.focus_today_date == current_key:
            return False

        previous_key = self.focus_today_date
        if not previous_key:
            self.focus_today_minutes = 0
        else:
            try:
                previous = date.fromisoformat(previous_key)
            except ValueError:
                previous = None
            if previous is None or previous < current:
                self.focus_today_minutes = 0
            # Future-dated minutes are intentionally retained.
        self.focus_today_date = current_key
        return True

    def record_focus_completion(
        self,
        minutes: int,
        today: date | None = None,
    ) -> int:
        """Record one finished plan and return the clamped duration."""
        duration = self._integer(
            minutes,
            1,
            minimum=1,
            maximum=self.MAX_FOCUS_PLANNED_MINUTES,
        )
        self.normalize_focus_today(today)
        self.focus_sessions_completed = min(
            self.MAX_FOCUS_SESSIONS,
            self.focus_sessions_completed + 1,
        )
        self.focus_minutes_completed = min(
            self.MAX_FOCUS_MINUTES,
            self.focus_minutes_completed + duration,
        )
        self.focus_today_minutes = min(
            self.MAX_FOCUS_TODAY_MINUTES,
            self.focus_today_minutes + duration,
        )
        return duration
