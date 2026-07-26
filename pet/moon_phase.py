"""Small, dependency-free lunar phase approximation.

The calculation is intentionally geocentric and location-independent. Aware
``datetime`` values are converted to UTC. Naive values are interpreted as UTC,
never as the machine's local timezone, so the same input is deterministic on
every computer.

This is suitable for an eight-phase companion UI, not for moonrise, visibility,
local disc orientation, or observatory-grade event timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import cos, floor, isfinite, pi
from typing import Final


SYNODIC_MONTH_DAYS: Final = 29.5305888531
"""Mean new-moon-to-new-moon period in days."""

NEW_MOON_EPOCH: Final = datetime(2000, 1, 6, 18, 15, tzinfo=UTC)
"""Reference new moon used by the approximation."""

NEW_MOON_EPOCH_JULIAN_DAY: Final = 2451550.2604166665

PHASE_NAMES: Final = (
    "新月",
    "盈月牙",
    "上弦月",
    "盈凸月",
    "满月",
    "亏凸月",
    "下弦月",
    "残月",
)

PHASE_EMOJIS: Final = ("🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘")

PRINCIPAL_PHASE_INDEXES: Final = frozenset({0, 2, 4, 6})

_UNIX_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_UNIX_EPOCH_JULIAN_DAY: Final = 2440587.5
_SECONDS_PER_DAY: Final = 86_400.0


@dataclass(frozen=True, slots=True)
class MoonPhase:
    """An approximate lunar phase at one instant.

    ``fraction`` is the completed fraction of the current mean synodic cycle,
    in ``[0, 1)``. ``event_key`` identifies the nearest eighth-phase event and
    remains stable around that event. Use :meth:`principal_event_key` when only
    new moon, first quarter, full moon, and last quarter should trigger copy.
    """

    fraction: float
    age_days: float
    illumination: float
    index: int
    name: str
    emoji: str
    event_key: str
    event_distance_days: float

    @property
    def is_principal(self) -> bool:
        """Whether this is nearest to one of the four principal phases."""

        return self.index in PRINCIPAL_PHASE_INDEXES

    def principal_event_key(self, *, within_days: float | None = None) -> str | None:
        """Return a stable key for a nearby principal phase, otherwise ``None``.

        ``within_days`` can narrow the trigger window. For example,
        ``within_days=0.75`` limits a message to roughly 18 hours either side of
        the approximated principal phase.
        """

        if within_days is not None:
            if not isfinite(within_days) or within_days < 0:
                raise ValueError("within_days must be a finite, non-negative number")
            if self.event_distance_days > within_days:
                return None
        return self.event_key if self.is_principal else None


def _as_utc(moment: datetime) -> datetime:
    if not isinstance(moment, datetime):
        raise TypeError("moment must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _julian_day(moment: datetime) -> float:
    utc_moment = _as_utc(moment)
    elapsed = utc_moment - _UNIX_EPOCH
    return _UNIX_EPOCH_JULIAN_DAY + elapsed.total_seconds() / _SECONDS_PER_DAY


def calculate_moon_phase(moment: datetime | None = None) -> MoonPhase:
    """Return the approximate eight-phase lunar state at ``moment``.

    If ``moment`` is omitted, the current UTC time is used. An aware value is
    converted to UTC; a naive value is explicitly interpreted as UTC.
    """

    utc_moment = datetime.now(UTC) if moment is None else _as_utc(moment)
    julian_day = _julian_day(utc_moment)
    cycles = (julian_day - NEW_MOON_EPOCH_JULIAN_DAY) / SYNODIC_MONTH_DAYS
    fraction = cycles % 1.0
    age_days = fraction * SYNODIC_MONTH_DAYS
    illumination = (1.0 - cos(2.0 * pi * fraction)) / 2.0
    illumination = min(1.0, max(0.0, illumination))

    nearest_eighth = floor(cycles * 8.0 + 0.5)
    index = nearest_eighth % 8
    event_distance_days = abs(cycles - nearest_eighth / 8.0) * SYNODIC_MONTH_DAYS

    return MoonPhase(
        fraction=fraction,
        age_days=age_days,
        illumination=illumination,
        index=index,
        name=PHASE_NAMES[index],
        emoji=PHASE_EMOJIS[index],
        event_key=str(nearest_eighth),
        event_distance_days=event_distance_days,
    )


__all__ = [
    "MoonPhase",
    "NEW_MOON_EPOCH",
    "NEW_MOON_EPOCH_JULIAN_DAY",
    "PHASE_EMOJIS",
    "PHASE_NAMES",
    "PRINCIPAL_PHASE_INDEXES",
    "SYNODIC_MONTH_DAYS",
    "calculate_moon_phase",
]
