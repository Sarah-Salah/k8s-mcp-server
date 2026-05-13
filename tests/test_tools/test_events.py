"""Tests for the ``list_events`` tool."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools.events import ListEventsInput, list_events

PATCH_TARGET = "k8s_mcp_server.tools.events"


def _event(
    *,
    type_: str = "Normal",
    reason: str = "Scheduled",
    message: str = "...",
    count: int = 1,
    age_minutes: int = 5,
    involved_kind: str = "Pod",
    involved_name: str = "api-1",
    involved_namespace: str = "dev",
) -> SimpleNamespace:
    last = datetime.now(UTC) - timedelta(minutes=age_minutes)
    first = last - timedelta(minutes=1)
    return SimpleNamespace(
        type=type_,
        reason=reason,
        message=message,
        count=count,
        last_timestamp=last,
        event_time=None,
        first_timestamp=first,
        metadata=SimpleNamespace(creation_timestamp=first),
        involved_object=SimpleNamespace(
            kind=involved_kind, name=involved_name, namespace=involved_namespace
        ),
    )


@pytest.fixture
def events_api(patch_core_v1: Callable[[str], MagicMock]) -> MagicMock:
    return patch_core_v1(PATCH_TARGET)


# ---------------------------------------------------------------------------
# Namespace dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_events_in_default_namespace_when_none(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[_event()])

    result = await list_events(ListEventsInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    events_api.list_namespaced_event.assert_called_once()
    assert events_api.list_namespaced_event.call_args.kwargs["namespace"] == "default"
    events_api.list_event_for_all_namespaces.assert_not_called()


@pytest.mark.asyncio
async def test_lists_events_in_specific_namespace(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[_event()])

    await list_events(ListEventsInput(namespace="staging"), ctx=kube_context, settings=Settings())

    assert events_api.list_namespaced_event.call_args.kwargs["namespace"] == "staging"


@pytest.mark.asyncio
async def test_all_no_allowlist_uses_list_event_for_all_namespaces(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_event_for_all_namespaces.return_value = SimpleNamespace(items=[_event()])

    result = await list_events(
        ListEventsInput(namespace="all"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    events_api.list_event_for_all_namespaces.assert_called_once()
    events_api.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_all_with_allowlist_iterates_allowlisted_namespaces(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.side_effect = [
        SimpleNamespace(items=[_event(reason="dev-a", involved_namespace="dev")]),
        SimpleNamespace(items=[_event(reason="staging-b", involved_namespace="staging")]),
    ]

    result = await list_events(
        ListEventsInput(namespace="all"),
        ctx=kube_context,
        settings=Settings(namespaces=("staging", "dev")),
    )

    assert result.success is True
    events_api.list_event_for_all_namespaces.assert_not_called()
    called_namespaces = [
        call.kwargs["namespace"] for call in events_api.list_namespaced_event.call_args_list
    ]
    # resolve_read_namespaces returns the allowlist sorted.
    assert called_namespaces == ["dev", "staging"]


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    result = await list_events(
        ListEventsInput(namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")
    events_api.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    result = await list_events(
        ListEventsInput(),  # no namespace; ctx default is "default"
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


# ---------------------------------------------------------------------------
# Field selector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_field_selector_built_from_kind_name_type(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await list_events(
        ListEventsInput(
            namespace="dev",
            involved_object_kind="Pod",
            involved_object_name="api-1",
            type="Warning",
        ),
        ctx=kube_context,
        settings=Settings(),
    )

    fs = events_api.list_namespaced_event.call_args.kwargs["field_selector"]
    assert fs == "involvedObject.kind=Pod,involvedObject.name=api-1,type=Warning"


@pytest.mark.asyncio
async def test_field_selector_is_none_when_no_filters(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await list_events(ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert events_api.list_namespaced_event.call_args.kwargs["field_selector"] is None


@pytest.mark.asyncio
async def test_only_kind_in_field_selector_when_only_kind_set(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await list_events(
        ListEventsInput(namespace="dev", involved_object_kind="Pod"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert (
        events_api.list_namespaced_event.call_args.kwargs["field_selector"]
        == "involvedObject.kind=Pod"
    )


# ---------------------------------------------------------------------------
# since_seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_since_seconds_filters_out_old_events(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    fresh = _event(reason="Fresh", age_minutes=1)
    stale = _event(reason="Stale", age_minutes=120)
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[fresh, stale])

    result = await list_events(
        ListEventsInput(namespace="dev", since_seconds=600),
        ctx=kube_context,
        settings=Settings(),
    )

    reasons = [e["reason"] for e in result.data["events"]]
    assert reasons == ["Fresh"]


@pytest.mark.asyncio
async def test_since_seconds_drops_events_with_no_timestamps(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    """Events that fall back to the epoch timestamp are far older than any
    real ``since_seconds`` window and should be filtered out."""
    real = _event(reason="Real", age_minutes=2)
    malformed = SimpleNamespace(
        type="Normal",
        reason="Malformed",
        message="...",
        count=1,
        last_timestamp=None,
        event_time=None,
        first_timestamp=None,
        metadata=None,
        involved_object=SimpleNamespace(kind="Pod", name="x", namespace="dev"),
    )
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[real, malformed])

    result = await list_events(
        ListEventsInput(namespace="dev", since_seconds=3600),
        ctx=kube_context,
        settings=Settings(),
    )

    reasons = [e["reason"] for e in result.data["events"]]
    assert reasons == ["Real"]


# ---------------------------------------------------------------------------
# Sorting / truncation / formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_sorted_most_recent_first(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(
        items=[
            _event(reason="Old", age_minutes=30),
            _event(reason="Newest", age_minutes=1),
            _event(reason="Middle", age_minutes=10),
        ]
    )

    result = await list_events(
        ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    reasons = [e["reason"] for e in result.data["events"]]
    assert reasons == ["Newest", "Middle", "Old"]


@pytest.mark.asyncio
async def test_events_truncated_at_limit_with_truncated_flag(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    items = [_event(reason=f"R{i}", age_minutes=i + 1) for i in range(20)]
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=items)

    result = await list_events(
        ListEventsInput(namespace="dev", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is True
    assert len(result.data["events"]) == 5
    assert [e["reason"] for e in result.data["events"]] == ["R0", "R1", "R2", "R3", "R4"]


@pytest.mark.asyncio
async def test_events_not_truncated_when_under_limit(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[_event(reason="Only")])

    result = await list_events(
        ListEventsInput(namespace="dev", limit=50),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is False
    assert len(result.data["events"]) == 1


@pytest.mark.asyncio
async def test_event_format_includes_all_required_fields(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.return_value = SimpleNamespace(
        items=[
            _event(
                type_="Warning",
                reason="BackOff",
                message="Back-off restarting",
                count=7,
                age_minutes=3,
                involved_kind="Pod",
                involved_name="api-7d4f9",
                involved_namespace="staging",
            )
        ]
    )

    result = await list_events(
        ListEventsInput(namespace="staging"), ctx=kube_context, settings=Settings()
    )

    [out] = result.data["events"]
    assert out["type"] == "Warning"
    assert out["reason"] == "BackOff"
    assert out["message"] == "Back-off restarting"
    assert out["count"] == 7
    assert out["first_seen_age_seconds"] is not None
    assert out["last_seen_age_seconds"] is not None
    assert out["involved_object"] == {
        "kind": "Pod",
        "name": "api-7d4f9",
        "namespace": "staging",
    }


@pytest.mark.asyncio
async def test_event_with_only_event_time_sorts_correctly(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    """Newer-API events have ``event_time`` instead of ``last_timestamp``."""
    older = _event(reason="Old", age_minutes=10)
    older.last_timestamp = None
    older.event_time = datetime.now(UTC) - timedelta(minutes=10)
    newer = _event(reason="New", age_minutes=2)
    newer.last_timestamp = None
    newer.event_time = datetime.now(UTC) - timedelta(minutes=2)
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[older, newer])

    result = await list_events(
        ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert [e["reason"] for e in result.data["events"]] == ["New", "Old"]


@pytest.mark.asyncio
async def test_event_with_no_timestamps_sorts_to_bottom(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    real = _event(reason="Real", age_minutes=2)
    malformed = SimpleNamespace(
        type="Normal",
        reason="Malformed",
        message="...",
        count=1,
        last_timestamp=None,
        event_time=None,
        first_timestamp=None,
        metadata=None,
        involved_object=None,
    )
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[malformed, real])

    result = await list_events(
        ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert [e["reason"] for e in result.data["events"]] == ["Real", "Malformed"]


@pytest.mark.asyncio
async def test_event_with_missing_involved_object_returns_nulls(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    """Defensive: malformed event with no involved_object should not crash."""
    e = _event(reason="X")
    e.involved_object = None
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[e])

    result = await list_events(
        ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    [out] = result.data["events"]
    assert out["involved_object"] == {"kind": None, "name": None, "namespace": None}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_error_on_api_exception(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")

    result = await list_events(
        ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "Forbidden" in (result.error or "")


@pytest.mark.asyncio
async def test_returns_error_on_unexpected_exception(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    events_api.list_namespaced_event.side_effect = RuntimeError("boom")

    result = await list_events(
        ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": "nope"},
        {"type": "Info"},  # not in Literal["Normal", "Warning"]
        {"type": "normal"},  # case-sensitive
        {"since_seconds": 0},
        {"limit": 0},
        {"limit": 1001},
    ],
)
def test_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ListEventsInput.model_validate(payload)


@pytest.mark.parametrize("type_", ["Normal", "Warning"])
def test_input_accepts_valid_type_values(type_: str) -> None:
    inp = ListEventsInput.model_validate({"type": type_})
    assert inp.type == type_


@pytest.mark.asyncio
async def test_event_with_only_metadata_creation_timestamp_sorts_correctly(
    kube_context: KubeContext, events_api: MagicMock
) -> None:
    """Defensive: events with no last_timestamp / event_time but a metadata
    creation_timestamp fall back to that for ordering."""
    older_creation = datetime.now(UTC) - timedelta(minutes=30)
    newer_creation = datetime.now(UTC) - timedelta(minutes=2)
    older = SimpleNamespace(
        type="Normal",
        reason="Old",
        message="...",
        count=1,
        last_timestamp=None,
        event_time=None,
        first_timestamp=None,
        metadata=SimpleNamespace(creation_timestamp=older_creation),
        involved_object=SimpleNamespace(kind="Pod", name="x", namespace="dev"),
    )
    newer = SimpleNamespace(
        type="Normal",
        reason="New",
        message="...",
        count=1,
        last_timestamp=None,
        event_time=None,
        first_timestamp=None,
        metadata=SimpleNamespace(creation_timestamp=newer_creation),
        involved_object=SimpleNamespace(kind="Pod", name="x", namespace="dev"),
    )
    events_api.list_namespaced_event.return_value = SimpleNamespace(items=[older, newer])

    result = await list_events(
        ListEventsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert [e["reason"] for e in result.data["events"]] == ["New", "Old"]
