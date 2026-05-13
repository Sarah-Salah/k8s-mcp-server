"""Direct unit tests for the public ``format_condition`` helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from k8s_mcp_server.utils.k8s_conditions import format_condition


def test_returns_all_fields_when_populated() -> None:
    transition = datetime.now(UTC) - timedelta(minutes=30)
    cond = SimpleNamespace(
        type="Ready",
        status="True",
        reason="KubeletReady",
        message="kubelet is posting ready status",
        last_transition_time=transition,
    )

    out = format_condition(cond)

    assert out["type"] == "Ready"
    assert out["status"] == "True"
    assert out["reason"] == "KubeletReady"
    assert out["message"] == "kubelet is posting ready status"
    assert out["last_transition_age_seconds"] >= 30 * 60


def test_last_transition_age_is_none_when_time_missing() -> None:
    cond = SimpleNamespace(
        type="Available",
        status="False",
        reason="MinimumReplicasUnavailable",
        message="...",
        last_transition_time=None,
    )

    out = format_condition(cond)

    assert out["last_transition_age_seconds"] is None


def test_reason_and_message_null_when_missing() -> None:
    cond = SimpleNamespace(
        type="Progressing",
        status="True",
        reason=None,
        message=None,
        last_transition_time=None,
    )

    out = format_condition(cond)

    assert out["reason"] is None
    assert out["message"] is None
    assert out["type"] == "Progressing"
    assert out["status"] == "True"


def test_handles_object_with_no_attributes() -> None:
    """Defensive: a condition-shaped object with none of the expected attrs
    returns an all-None dict (every field defaulting via getattr)."""
    out = format_condition(SimpleNamespace())

    assert out == {
        "type": None,
        "status": None,
        "reason": None,
        "message": None,
        "last_transition_age_seconds": None,
    }
