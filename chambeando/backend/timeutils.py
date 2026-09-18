"""Application-wide time convention: all timestamps are UTC-aware.

Columns are declared DateTime(timezone=True). PostgreSQL returns aware
datetimes for these natively; SQLite always returns naive datetimes
regardless of the column declaration (driver limitation), so any value
read back from the DB must be passed through ensure_utc() before it is
compared against another datetime.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime (e.g. read back from SQLite) to UTC-aware.

    A naive value is assumed to already be UTC (the only value ever written).
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
