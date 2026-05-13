"""Output formatting helpers: age calculation and human-friendly age strings."""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["age_human", "age_seconds_since"]


def age_seconds_since(timestamp: datetime | None, *, now: datetime | None = None) -> int:
    """Return seconds elapsed since ``timestamp``.

    Naive datetimes are assumed UTC. ``None`` returns 0. Timestamps in the
    future are clamped to 0 (clock skew between client and cluster).
    """
    if timestamp is None:
        return 0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0, int((reference - timestamp).total_seconds()))


def age_human(seconds: int) -> str:
    """Format a duration the way ``kubectl`` does: ``45s``, ``3h12m``, ``5d``, ``2y10d``."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m{secs}s"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if mins == 0 else f"{hours}h{mins}m"
    days, hrs = divmod(hours, 24)
    if days < 365:
        return f"{days}d" if hrs == 0 else f"{days}d{hrs}h"
    years, dys = divmod(days, 365)
    return f"{years}y" if dys == 0 else f"{years}y{dys}d"
