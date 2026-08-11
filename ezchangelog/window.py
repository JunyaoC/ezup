"""Relative time-window parsing and timestamp helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_duration(text: str) -> timedelta:
    """Parse a relative duration such as ``7d``, ``24h``, ``90m``, ``2w``."""
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError(
            f"invalid duration {text!r}; expected forms like 7d, 24h, 90m, 2w"
        )
    amount, unit = match.groups()
    return timedelta(seconds=float(amount) * _UNIT_SECONDS[unit.lower()])


def since_to_datetime(text: str, now: datetime | None = None) -> datetime:
    """Resolve ``--since`` into an absolute UTC cutoff.

    Accepts a relative duration (``7d``) or an absolute ISO-8601 instant.
    """
    now = now or datetime.now(timezone.utc)
    try:
        return now - parse_duration(text)
    except ValueError:
        pass
    parsed = parse_calendar(text)
    if parsed is None:
        raise ValueError(
            f"invalid time {text!r}; expected a duration (7d), a date "
            f"(2026-08-01), or an ISO-8601 timestamp"
        )
    return parsed


def parse_calendar(text: str) -> datetime | None:
    """Parse a plain date or timestamp, treating a bare one as local time.

    ``2026-08-01`` means midnight where the user is, not midnight UTC.
    """
    raw = text.strip()
    if not raw:
        return None
    body = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(body)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def end_of_day(moment: datetime) -> datetime:
    """Push a local midnight to the end of that local day."""
    local = moment.astimezone()
    if (local.hour, local.minute, local.second) != (0, 0, 0):
        return moment
    return (local + timedelta(days=1)).astimezone(timezone.utc)


def parse_timestamp(value: object) -> datetime | None:
    """Parse a transcript timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
