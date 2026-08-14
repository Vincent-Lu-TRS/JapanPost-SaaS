"""Small, dependency-free helpers for the application's user-facing clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# Japan does not observe daylight saving time, so a fixed UTC+9 offset is
# sufficient and avoids relying on a host-specific tzdata installation.
JST = timezone(timedelta(hours=9), name="Asia/Tokyo")


def as_jst(value: datetime | None) -> datetime | None:
    """Return ``value`` as an aware Japan-local datetime.

    Naive timestamps in this application were created by a local UI call, so
    they are interpreted as already being Japan-local rather than guessed as
    UTC. New timestamps are created aware and therefore convert normally.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=JST)
    return value.astimezone(JST)


def format_jst(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a timestamp for users without exposing a timezone suffix."""
    converted = as_jst(value)
    return converted.strftime(fmt) if converted is not None else ""
