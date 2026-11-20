"""Five-field cron: parse + next-slot, DST fall-back safe (T-C3-003).

Upstream 2.3.2 schedules cron triggers through a cron library whose
``next`` maps a repeated wall-clock time (DST fall-back) to its FIRST
occurrence, so a slot whose first occurrence is already past never fires a
second time during the repeated hour. This module reproduces that contract
with no new dependency (repo discipline):

- five numeric fields (minute hour day-of-month month day-of-week), each
  ``*``, ``a``, ``a-b``, ``a-b/n``, ``*/n`` or comma combinations; day-of-
  week 7 normalizes to 0 (Sunday). Names/macros are out of first-period
  scope (recorded deviation);
- standard cron day semantics: when both day-of-month and day-of-week are
  restricted, a date matches if EITHER matches; when only one is
  restricted, that one must match;
- ``next_after`` scans local days (bounded at 5 years -- a never-matching
  expression returns ``None``) and within a day the valid (hour, minute)
  slots in chronological order. Each candidate is a wall-clock time in the
  trigger's zone with ``fold=0`` (the first occurrence); a spring-forward
  gap time does not exist as an instant and is skipped (round-trip check).

All timestamps are epoch milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Upper bound of the day scan: past this, the expression never matches.
_MAX_SCAN_DAYS = 5 * 366

_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 6),
)


class InvalidCronError(ValueError):
    """The expression is not a valid five-field cron."""


@dataclass(frozen=True, slots=True)
class CronSpec:
    """Parsed sets; the ``*_star`` flags keep track of unrestricted fields."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    day_star: bool
    dow_star: bool


def _parse_part(part: str, low: int, high: int) -> tuple[set[int], bool]:
    fields = part.split("/")
    if len(fields) > 2:
        raise InvalidCronError(f"too many '/' in {part!r}")
    range_text = fields[0]
    step = int(fields[1]) if len(fields) == 2 and fields[1] else 1
    if step < 1:
        raise InvalidCronError(f"step must be >= 1 in {part!r}")
    star = range_text == "*"
    if star:
        start, end = low, high
    elif "-" in range_text:
        first, last = range_text.split("-", maxsplit=1)
        try:
            start, end = int(first), int(last)
        except ValueError as error:
            raise InvalidCronError(f"bad range {range_text!r}") from error
    else:
        try:
            start = end = int(range_text)
        except ValueError as error:
            raise InvalidCronError(f"bad value {range_text!r}") from error
    if start < low or end > high or start > end:
        raise InvalidCronError(f"{part!r} out of range {low}-{high}")
    values = set(range(start, end + 1, step))
    return values, star


def parse(expression: str) -> CronSpec:
    parts = expression.split()
    if len(parts) != 5:
        raise InvalidCronError("cron must have exactly five fields")
    sets: list[set[int]] = []
    stars: list[bool] = []
    for part, (_, low, high) in zip(parts, _FIELDS, strict=True):
        values: set[int] = set()
        star = True
        for piece in part.split(","):
            piece_values, piece_star = _parse_part(piece, low, high)
            values |= piece_values
            star = star and piece_star
        sets.append(values)
        stars.append(star)
    # 7 is an alias for Sunday, matching the crontab convention.
    dows = {0 if value == 7 else value for value in sets[4]}
    return CronSpec(
        minutes=frozenset(sets[0]),
        hours=frozenset(sets[1]),
        days=frozenset(sets[2]),
        months=frozenset(sets[3]),
        dows=frozenset(dows),
        day_star=stars[2],
        dow_star=stars[4],
    )


def _day_matches(spec: CronSpec, day: date) -> bool:
    if day.month not in spec.months:
        return False
    dom_ok = day.day in spec.days
    # cron numbering is Sunday=0; datetime.weekday() is Monday=0.
    dow_ok = (day.weekday() + 1) % 7 in spec.dows
    if spec.day_star and spec.dow_star:
        return True
    if spec.day_star:
        return dow_ok
    if spec.dow_star:
        return dom_ok
    return dom_ok or dow_ok


def _instant_exists(local: datetime) -> bool:
    """False for a wall-clock time inside a spring-forward gap."""
    zone = local.tzinfo
    if zone is None:
        return True
    back = local.astimezone(UTC).astimezone(zone)
    return back.timetuple()[:5] == local.timetuple()[:5]


def next_after(spec: CronSpec, tz_name: str, now_ms: int) -> int | None:
    """The first slot STRICTLY after ``now_ms``, or None if there is none.

    A repeated wall-clock time (DST fall-back) maps to its first occurrence
    (``fold=0``), so during the repeated hour an already-fired slot is past
    and the scan steps to the next matching slot (upstream contract).
    """
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise InvalidCronError(f"unknown time zone {tz_name!r}") from error
    day = datetime.fromtimestamp(now_ms / 1000, zone).date()
    for offset in range(_MAX_SCAN_DAYS):
        candidate_day = day + timedelta(days=offset)
        if not _day_matches(spec, candidate_day):
            continue
        for hour in sorted(spec.hours):
            for minute in sorted(spec.minutes):
                local = datetime(
                    candidate_day.year,
                    candidate_day.month,
                    candidate_day.day,
                    hour,
                    minute,
                    tzinfo=zone,
                    fold=0,
                )
                if not _instant_exists(local):
                    continue
                at_ms = int(local.timestamp() * 1000)
                if at_ms > now_ms:
                    return at_ms
    return None
