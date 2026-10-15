"""Time helpers.

Rules of the system:
* All *stored* timestamps are ISO-8601 strings in UTC unless they are
  device-reported event timestamps, which are stored verbatim together with
  the configured device timezone label (never silently converted).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """Parse ISO strings produced by this app (tolerates naive values as UTC)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def seconds_from_now(seconds: float) -> str:
    return iso_utc(utcnow() + timedelta(seconds=seconds))


def format_device_ts(dt: datetime) -> str:
    """Canonical storage form for a device-reported local timestamp.

    Stored as naive local time (as the terminal reported it) with
    second precision, e.g. ``2026-10-15T08:31:02``. The timezone label is
    stored separately in ``event_timestamp_tz`` — we never re-interpret or
    shift the value.
    """
    return dt.replace(microsecond=0, tzinfo=None).isoformat(timespec="seconds")
