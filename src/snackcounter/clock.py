from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Deterministic clock. Tests and the demo replay time without sleeping."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware datetime")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(timezone.utc)

    def advance(self, **kwargs: float) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def local_date(value: datetime, tz_name: str) -> date:
    return value.astimezone(ZoneInfo(tz_name)).date()


def local_dt(value: datetime, tz_name: str) -> datetime:
    return value.astimezone(ZoneInfo(tz_name))


def fmt_local(value: datetime, tz_name: str) -> str:
    return local_dt(value, tz_name).strftime("%a %d %b %H:%M")


def parse_holidays(raw: str) -> set[date]:
    import json

    try:
        return {date.fromisoformat(item) for item in json.loads(raw or "[]")}
    except (ValueError, TypeError):
        return set()


def next_business_deadline(
    now: datetime,
    tz_name: str,
    holidays: set[date],
    *,
    due_hour: int = 18,
    business_days: int = 1,
) -> datetime:
    """End of the next business day, in the office's own calendar and timezone.

    Oslo and Bengaluru have different weekends and public holidays, so a single
    global "24 hours" timer would be wrong in both places.
    """
    tz = ZoneInfo(tz_name)
    cursor = now.astimezone(tz).date()
    counted = 0
    while counted < business_days:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in holidays:
            counted += 1
    deadline_local = datetime.combine(cursor, time(hour=due_hour), tzinfo=tz)
    return deadline_local.astimezone(timezone.utc)
