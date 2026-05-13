"""Direct unit tests for the public ``event_sort_key`` helper."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from k8s_mcp_server.utils.k8s_events import event_sort_key


def test_uses_last_timestamp_when_set() -> None:
    last = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    event_time = datetime(2026, 5, 13, 9, 0, 0, tzinfo=UTC)
    creation = datetime(2026, 5, 13, 8, 0, 0, tzinfo=UTC)
    event = SimpleNamespace(
        last_timestamp=last,
        event_time=event_time,
        metadata=SimpleNamespace(creation_timestamp=creation),
    )

    assert event_sort_key(event) == last


def test_falls_back_to_event_time_when_last_timestamp_is_none() -> None:
    event_time = datetime(2026, 5, 13, 9, 0, 0, tzinfo=UTC)
    creation = datetime(2026, 5, 13, 8, 0, 0, tzinfo=UTC)
    event = SimpleNamespace(
        last_timestamp=None,
        event_time=event_time,
        metadata=SimpleNamespace(creation_timestamp=creation),
    )

    assert event_sort_key(event) == event_time


def test_falls_back_to_creation_timestamp_when_both_are_none() -> None:
    creation = datetime(2026, 5, 13, 8, 0, 0, tzinfo=UTC)
    event = SimpleNamespace(
        last_timestamp=None,
        event_time=None,
        metadata=SimpleNamespace(creation_timestamp=creation),
    )

    assert event_sort_key(event) == creation


def test_falls_back_to_epoch_when_everything_is_none() -> None:
    event = SimpleNamespace(
        last_timestamp=None,
        event_time=None,
        metadata=SimpleNamespace(creation_timestamp=None),
    )

    assert event_sort_key(event) == datetime(1970, 1, 1, tzinfo=UTC)


def test_falls_back_to_epoch_when_metadata_is_none() -> None:
    """Defensive: malformed event with metadata=None should return epoch, not crash."""
    event = SimpleNamespace(last_timestamp=None, event_time=None, metadata=None)

    assert event_sort_key(event) == datetime(1970, 1, 1, tzinfo=UTC)
