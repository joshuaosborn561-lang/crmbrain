"""Alarm when a source has not produced data for more than two business days."""

from __future__ import annotations

from datetime import datetime, timedelta

from crmbrain.config import CDT, now_utc
from crmbrain.models import CycleReport

WATCHED_SOURCES = ("gmail", "fireflies", "calendar", "allo", "smartlead")
STALE_BUSINESS_DAYS = 2


def add_business_days(start: datetime, days: int) -> datetime:
    cursor = start.astimezone(CDT)
    stepped = 0
    while stepped < days:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            stepped += 1
    return cursor.astimezone(start.tzinfo or CDT)


def business_days_between(start: datetime, end: datetime) -> int:
    if end < start:
        return 0
    days = 0
    cursor = start.astimezone(CDT).date()
    last = end.astimezone(CDT).date()
    while cursor < last:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def is_stale(last_item_at: datetime | None, now: datetime | None = None, *, days: int = STALE_BUSINESS_DAYS) -> bool:
    now = now or now_utc()
    if last_item_at is None:
        return True
    if last_item_at.tzinfo is None:
        last_item_at = last_item_at.replace(tzinfo=now.tzinfo)
    return business_days_between(last_item_at, now) > days


def record_and_alarm(
    memory,
    report: CycleReport,
    observed: dict[str, datetime | None],
    *,
    now: datetime | None = None,
    errors: dict[str, str] | None = None,
) -> list[str]:
    """Persist last-seen timestamps and append stale_sources on the report."""
    now = now or now_utc()
    errors = errors or {}
    stale: list[str] = []
    for source in WATCHED_SOURCES:
        last = observed.get(source)
        err = errors.get(source, "")
        if hasattr(memory, "record_freshness"):
            memory.record_freshness(source, last_item_at=last, last_error=err, when=now)
        stored = last
        if stored is None and hasattr(memory, "latest_freshness"):
            stored = memory.latest_freshness(source)
        if is_stale(stored, now):
            when = stored.astimezone(CDT).isoformat() if stored else "never"
            line = f"{source} stale (last item {when})"
            stale.append(line)
            report.stale_sources.append(line)
    return stale
