"""Helpers for working with Kubernetes ``V1Event`` objects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

__all__ = ["event_sort_key"]


def event_sort_key(event: Any) -> datetime:
    """Most-recent timestamp on a Kubernetes ``V1Event`` for ordering.

    Resolution order:
        1. ``event.last_timestamp``
        2. ``event.event_time`` (the newer events.k8s.io API uses this)
        3. ``event.metadata.creation_timestamp``
        4. ``datetime(1970, 1, 1, tzinfo=UTC)`` — epoch fallback for malformed
           events with no usable timestamps; sorts to the bottom in
           reverse-chronological order and falls outside any reasonable
           ``since_seconds`` window.

    Used by tools that emit lists of events (currently ``get_pod``'s embedded
    events and ``list_events``).
    """
    for attr in ("last_timestamp", "event_time"):
        value = getattr(event, attr, None)
        if value is not None:
            return cast(datetime, value)
    metadata = getattr(event, "metadata", None)
    if metadata is not None:
        ct = getattr(metadata, "creation_timestamp", None)
        if ct is not None:
            return cast(datetime, ct)
    return datetime(1970, 1, 1, tzinfo=UTC)
