"""Tests for the ``list_pods`` and ``get_pod`` tools."""

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
from k8s_mcp_server.tools.pods import GetPodInput, ListPodsInput, get_pod, list_pods

PATCH_TARGET = "k8s_mcp_server.tools.pods"


def _container(name: str, *, ready: bool = True, restarts: int = 0) -> SimpleNamespace:
    return SimpleNamespace(name=name, ready=ready, restart_count=restarts)


def _pod(
    name: str,
    *,
    namespace: str = "default",
    phase: str = "Running",
    age_minutes: int = 30,
    node: str | None = "node-1",
    pod_ip: str | None = "10.0.0.1",
    containers: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace, creation_timestamp=created),
        spec=SimpleNamespace(node_name=node),
        status=SimpleNamespace(
            phase=phase,
            pod_ip=pod_ip,
            container_statuses=containers or [_container("app")],
        ),
    )


@pytest.fixture
def pods_api(patch_core_v1: Callable[[str], MagicMock]) -> MagicMock:
    return patch_core_v1(PATCH_TARGET)


@pytest.mark.asyncio
async def test_specific_namespace_calls_list_namespaced_pod(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod("a", namespace="dev")])

    result = await list_pods(ListPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is True
    pods_api.list_namespaced_pod.assert_called_once()
    assert pods_api.list_namespaced_pod.call_args.kwargs["namespace"] == "dev"
    pods_api.list_pod_for_all_namespaces.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_none_uses_context_default(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_namespaced_pod.return_value = SimpleNamespace(items=[])

    result = await list_pods(ListPodsInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert pods_api.list_namespaced_pod.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_all_no_allowlist_calls_list_pod_for_all_namespaces(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(
        items=[_pod("a", namespace="default"), _pod("b", namespace="kube-system")]
    )

    result = await list_pods(ListPodsInput(namespace="all"), ctx=kube_context, settings=Settings())

    assert result.success is True
    pods_api.list_pod_for_all_namespaces.assert_called_once()
    pods_api.list_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_all_with_allowlist_iterates_allowlisted_namespaces(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_namespaced_pod.side_effect = [
        SimpleNamespace(items=[_pod("dev-a", namespace="dev")]),
        SimpleNamespace(items=[_pod("staging-b", namespace="staging")]),
    ]

    result = await list_pods(
        ListPodsInput(namespace="all"),
        ctx=kube_context,
        settings=Settings(namespaces=("staging", "dev")),
    )

    assert result.success is True
    pods_api.list_pod_for_all_namespaces.assert_not_called()
    called_namespaces = [
        call.kwargs["namespace"] for call in pods_api.list_namespaced_pod.call_args_list
    ]
    # Allowlist is iterated in sorted order (resolve_read_namespaces sorts).
    assert called_namespaces == ["dev", "staging"]
    names = [p["name"] for p in result.data["pods"]]
    assert names == ["dev-a", "staging-b"]


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_rejected_with_clear_error(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    result = await list_pods(
        ListPodsInput(namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")
    pods_api.list_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_default_namespace_outside_allowlist_rejected_when_omitted(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    result = await list_pods(
        ListPodsInput(),  # no namespace; ctx default is "default"
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")
    pods_api.list_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_label_selector_passed_to_api(kube_context: KubeContext, pods_api: MagicMock) -> None:
    pods_api.list_namespaced_pod.return_value = SimpleNamespace(items=[])

    await list_pods(
        ListPodsInput(namespace="dev", label_selector="app=nginx,tier=frontend"),
        ctx=kube_context,
        settings=Settings(),
    )

    kwargs = pods_api.list_namespaced_pod.call_args.kwargs
    assert kwargs["label_selector"] == "app=nginx,tier=frontend"


@pytest.mark.asyncio
async def test_field_selector_passed_to_api(kube_context: KubeContext, pods_api: MagicMock) -> None:
    pods_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])

    await list_pods(
        ListPodsInput(namespace="all", field_selector="status.phase=Running"),
        ctx=kube_context,
        settings=Settings(),
    )

    kwargs = pods_api.list_pod_for_all_namespaces.call_args.kwargs
    assert kwargs["field_selector"] == "status.phase=Running"


@pytest.mark.asyncio
async def test_truncated_true_when_results_exceed_limit(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(
        items=[_pod(f"p{i}") for i in range(10)]
    )

    result = await list_pods(
        ListPodsInput(namespace="all", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["truncated"] is True
    assert len(result.data["pods"]) == 5


@pytest.mark.asyncio
async def test_truncated_false_when_results_fit_in_limit(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(
        items=[_pod(f"p{i}") for i in range(3)]
    )

    result = await list_pods(
        ListPodsInput(namespace="all", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is False
    assert len(result.data["pods"]) == 3


@pytest.mark.asyncio
async def test_pods_sorted_by_namespace_then_name(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(
        items=[
            _pod("zeta", namespace="dev"),
            _pod("alpha", namespace="prod"),
            _pod("beta", namespace="dev"),
        ]
    )

    result = await list_pods(ListPodsInput(namespace="all"), ctx=kube_context, settings=Settings())

    sequence = [(p["namespace"], p["name"]) for p in result.data["pods"]]
    assert sequence == [("dev", "beta"), ("dev", "zeta"), ("prod", "alpha")]


@pytest.mark.asyncio
async def test_pod_format_includes_all_required_fields(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pod = _pod(
        "api-7d4f9",
        namespace="staging",
        phase="Running",
        age_minutes=120,
        node="node-3",
        pod_ip="10.0.5.42",
        containers=[
            _container("app", ready=True, restarts=2),
            _container("sidecar", ready=False, restarts=1),
        ],
    )
    pods_api.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])

    result = await list_pods(
        ListPodsInput(namespace="staging"), ctx=kube_context, settings=Settings()
    )

    [out] = result.data["pods"]
    assert out["name"] == "api-7d4f9"
    assert out["namespace"] == "staging"
    assert out["phase"] == "Running"
    assert out["ready"] == "1/2"
    assert out["restarts"] == 3  # sum across containers
    assert out["age_seconds"] >= 120 * 60
    assert "h" in out["age_human"]
    assert out["node"] == "node-3"
    assert out["pod_ip"] == "10.0.5.42"


@pytest.mark.asyncio
async def test_pod_with_no_containers_returns_zero_zero_ready(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pod = _pod("pending", containers=[])
    pod.status.container_statuses = None  # K8s often returns None pre-scheduling
    pods_api.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])

    result = await list_pods(
        ListPodsInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    [out] = result.data["pods"]
    assert out["ready"] == "0/0"
    assert out["restarts"] == 0


@pytest.mark.asyncio
async def test_pod_with_missing_metadata_does_not_crash(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    """Defensive: real K8s never returns None metadata, but mocks/partial objects might."""
    weird_pod: Any = SimpleNamespace(metadata=None, spec=None, status=None)
    pods_api.list_namespaced_pod.return_value = SimpleNamespace(items=[weird_pod])

    result = await list_pods(
        ListPodsInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    [out] = result.data["pods"]
    assert out["name"] == "Unknown"
    assert out["namespace"] == "Unknown"
    assert out["phase"] == "Unknown"
    assert out["ready"] == "0/0"
    assert out["node"] is None
    assert out["pod_ip"] is None


@pytest.mark.asyncio
async def test_returns_error_on_api_exception(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

    result = await list_pods(ListPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "Forbidden" in (result.error or "")


@pytest.mark.asyncio
async def test_returns_error_on_unexpected_exception(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.list_namespaced_pod.side_effect = RuntimeError("boom")

    result = await list_pods(ListPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "boom" in (result.error or "")


def test_input_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ListPodsInput.model_validate({"namespace": "dev", "extra": "x"})


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_input_rejects_invalid_limit(limit: int) -> None:
    with pytest.raises(ValidationError):
        ListPodsInput.model_validate({"limit": limit})


# ---------------------------------------------------------------------------
# get_pod
# ---------------------------------------------------------------------------


def _container_status(
    name: str,
    *,
    image: str = "nginx:1.25",
    ready: bool = True,
    restarts: int = 0,
    state: SimpleNamespace | None = None,
) -> SimpleNamespace:
    if state is None:
        state = SimpleNamespace(
            running=SimpleNamespace(started_at=datetime.now(UTC) - timedelta(minutes=5)),
            waiting=None,
            terminated=None,
        )
    return SimpleNamespace(name=name, image=image, ready=ready, restart_count=restarts, state=state)


def _running_state() -> SimpleNamespace:
    return SimpleNamespace(
        running=SimpleNamespace(started_at=datetime.now(UTC)),
        waiting=None,
        terminated=None,
    )


def _waiting_state(reason: str, message: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        running=None,
        waiting=SimpleNamespace(reason=reason, message=message),
        terminated=None,
    )


def _terminated_state(
    reason: str, message: str | None = None, exit_code: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        running=None,
        waiting=None,
        terminated=SimpleNamespace(reason=reason, message=message, exit_code=exit_code),
    )


def _condition(
    type_: str,
    status: str,
    *,
    reason: str | None = None,
    message: str | None = None,
    age_minutes: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=type_,
        status=status,
        reason=reason,
        message=message,
        last_transition_time=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


def _event(
    *,
    type_: str = "Normal",
    reason: str = "Scheduled",
    message: str = "...",
    count: int = 1,
    age_minutes: int = 5,
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
    )


def _detailed_pod(
    name: str = "api-7d4f9",
    *,
    namespace: str = "staging",
    phase: str = "Running",
    age_minutes: int = 120,
    node: str | None = "node-3",
    pod_ip: str | None = "10.0.5.42",
    containers: list[SimpleNamespace] | None = None,
    init_containers: list[SimpleNamespace] | None = None,
    conditions: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace, creation_timestamp=created),
        spec=SimpleNamespace(node_name=node),
        status=SimpleNamespace(
            phase=phase,
            pod_ip=pod_ip,
            container_statuses=containers if containers is not None else [_container_status("app")],
            init_container_statuses=init_containers,
            conditions=conditions,
        ),
    )


@pytest.mark.asyncio
async def test_get_pod_returns_full_pod_detail(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pod = _detailed_pod(
        containers=[_container_status("app", image="api:1.4", ready=True, restarts=2)],
        conditions=[_condition("Ready", "True", age_minutes=110)],
    )
    pods_api.read_namespaced_pod.return_value = pod
    pods_api.list_namespaced_event.return_value = SimpleNamespace(
        items=[_event(reason="Scheduled"), _event(reason="Pulled", age_minutes=4)]
    )

    result = await get_pod(
        GetPodInput(name="api-7d4f9", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    data = result.data
    assert data["name"] == "api-7d4f9"
    assert data["namespace"] == "staging"
    assert data["phase"] == "Running"
    assert data["node"] == "node-3"
    assert data["pod_ip"] == "10.0.5.42"
    assert data["age_seconds"] >= 120 * 60
    assert "h" in data["age_human"]
    assert len(data["containers"]) == 1
    assert data["containers"][0]["name"] == "app"
    assert data["containers"][0]["image"] == "api:1.4"
    assert data["containers"][0]["ready"] is True
    assert data["containers"][0]["restart_count"] == 2
    assert data["containers"][0]["state"]["phase"] == "running"
    assert data["init_containers"] == []
    assert data["conditions"] == [
        {
            "type": "Ready",
            "status": "True",
            "reason": None,
            "message": None,
            "last_transition_age_seconds": data["conditions"][0]["last_transition_age_seconds"],
        }
    ]
    assert data["conditions"][0]["last_transition_age_seconds"] >= 110 * 60
    assert len(data["events"]) == 2


@pytest.mark.asyncio
async def test_get_pod_uses_default_namespace_when_none(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.return_value = _detailed_pod(namespace="default")
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await get_pod(GetPodInput(name="x"), ctx=kube_context, settings=Settings())

    assert pods_api.read_namespaced_pod.call_args.kwargs["namespace"] == "default"
    assert pods_api.list_namespaced_event.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_get_pod_specific_namespace_passes_through(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.return_value = _detailed_pod(namespace="dev")
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await get_pod(GetPodInput(name="x", namespace="dev"), ctx=kube_context, settings=Settings())

    assert pods_api.read_namespaced_pod.call_args.kwargs["namespace"] == "dev"


@pytest.mark.asyncio
async def test_get_pod_rejects_namespace_all(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    result = await get_pod(
        GetPodInput(name="x", namespace="all"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "single namespace" in (result.error or "")
    pods_api.read_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_get_pod_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    result = await get_pod(
        GetPodInput(name="x", namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")
    pods_api.read_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_get_pod_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    result = await get_pod(
        GetPodInput(name="x"),  # no namespace; ctx default is "default"
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")
    pods_api.read_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_get_pod_404_returns_friendly_not_found_error(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

    result = await get_pod(
        GetPodInput(name="ghost", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "ghost" in (result.error or "")
    assert "staging" in (result.error or "")
    assert "not found" in (result.error or "")
    pods_api.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_get_pod_non_404_api_error_returns_kubernetes_api_error(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.side_effect = ApiException(status=500, reason="Internal")

    result = await get_pod(
        GetPodInput(name="x", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")


@pytest.mark.asyncio
async def test_get_pod_unexpected_exception_returns_error(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.side_effect = RuntimeError("boom")

    result = await get_pod(
        GetPodInput(name="x", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_get_pod_event_field_selector_targets_pod(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.return_value = _detailed_pod(name="api-1", namespace="staging")
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await get_pod(
        GetPodInput(name="api-1", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    kwargs = pods_api.list_namespaced_event.call_args.kwargs
    assert kwargs["namespace"] == "staging"
    assert kwargs["field_selector"] == "involvedObject.kind=Pod,involvedObject.name=api-1"


@pytest.mark.asyncio
async def test_get_pod_events_sorted_by_last_seen_desc_and_capped_at_10(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.return_value = _detailed_pod()
    # 15 events with ages 1, 2, ..., 15 minutes (newer = smaller age)
    events = [_event(reason=f"Reason{i}", age_minutes=i) for i in range(1, 16)]
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=events)

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    out_events = result.data["events"]
    assert len(out_events) == 10
    # Most recent first: ages should be ascending in last_seen_age_seconds
    ages = [e["last_seen_age_seconds"] for e in out_events]
    assert ages == sorted(ages)
    # Reason1 (newest) should be first; Reason10 should be last in the cap.
    assert out_events[0]["reason"] == "Reason1"
    assert out_events[-1]["reason"] == "Reason10"


@pytest.mark.asyncio
async def test_get_pod_event_fetch_failure_returns_empty_events_but_keeps_pod_data(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.return_value = _detailed_pod(name="api-1", namespace="staging")
    pods_api.list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")

    result = await get_pod(
        GetPodInput(name="api-1", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.error is None
    assert result.data["name"] == "api-1"
    assert result.data["events"] == []


@pytest.mark.asyncio
async def test_get_pod_event_unexpected_exception_returns_empty_events(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pods_api.read_namespaced_pod.return_value = _detailed_pod()
    pods_api.list_namespaced_event.side_effect = RuntimeError("event boom")

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    assert result.data["events"] == []


@pytest.mark.asyncio
async def test_get_pod_formats_container_states(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pod = _detailed_pod(
        containers=[
            _container_status("running-c", state=_running_state()),
            _container_status(
                "waiting-c",
                ready=False,
                state=_waiting_state("CrashLoopBackOff", "Back-off restarting"),
            ),
            _container_status(
                "terminated-c",
                ready=False,
                restarts=3,
                state=_terminated_state("Error", "Exit 137", exit_code=137),
            ),
        ]
    )
    pods_api.read_namespaced_pod.return_value = pod
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    states = {c["name"]: c["state"] for c in result.data["containers"]}
    assert states["running-c"] == {"phase": "running", "reason": None, "message": None}
    assert states["waiting-c"] == {
        "phase": "waiting",
        "reason": "CrashLoopBackOff",
        "message": "Back-off restarting",
    }
    assert states["terminated-c"] == {
        "phase": "terminated",
        "reason": "Error",
        "message": "Exit 137",
    }


@pytest.mark.asyncio
async def test_get_pod_includes_init_containers(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    pod = _detailed_pod(
        init_containers=[
            _container_status("init-db", ready=False, state=_waiting_state("PodInitializing"))
        ]
    )
    pods_api.read_namespaced_pod.return_value = pod
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    assert len(result.data["init_containers"]) == 1
    assert result.data["init_containers"][0]["name"] == "init-db"
    assert result.data["init_containers"][0]["state"]["reason"] == "PodInitializing"


@pytest.mark.asyncio
async def test_get_pod_with_missing_metadata_status_does_not_crash(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    weird: Any = SimpleNamespace(metadata=None, spec=None, status=None)
    pods_api.read_namespaced_pod.return_value = weird
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    data = result.data
    assert data["name"] == "Unknown"
    assert data["namespace"] == "Unknown"
    assert data["phase"] == "Unknown"
    assert data["node"] is None
    assert data["pod_ip"] is None
    assert data["containers"] == []
    assert data["init_containers"] == []
    assert data["conditions"] == []


@pytest.mark.asyncio
async def test_get_pod_event_with_only_event_time_sorts_correctly(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    """Defensive: events emitted by newer event API have event_time, not last_timestamp."""
    pods_api.read_namespaced_pod.return_value = _detailed_pod()
    older = _event(age_minutes=10)
    older.last_timestamp = None
    older.event_time = datetime.now(UTC) - timedelta(minutes=10)
    older.reason = "Old"
    newer = _event(age_minutes=2)
    newer.last_timestamp = None
    newer.event_time = datetime.now(UTC) - timedelta(minutes=2)
    newer.reason = "New"
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[older, newer])

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    assert [e["reason"] for e in result.data["events"]] == ["New", "Old"]


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing name
        {"name": ""},  # empty name
        {"name": "x", "extra": "nope"},  # extra field
    ],
)
def test_get_pod_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GetPodInput.model_validate(payload)


@pytest.mark.asyncio
async def test_get_pod_event_sort_falls_back_to_creation_timestamp(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    """Defensive: events with neither last_timestamp nor event_time fall back
    to ``metadata.creation_timestamp`` for ordering."""
    pods_api.read_namespaced_pod.return_value = _detailed_pod()

    older_creation = datetime.now(UTC) - timedelta(minutes=20)
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
    )
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[older, newer])

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    assert [e["reason"] for e in result.data["events"]] == ["New", "Old"]


@pytest.mark.asyncio
async def test_get_pod_event_with_no_timestamps_sorts_to_bottom(
    kube_context: KubeContext, pods_api: MagicMock
) -> None:
    """Defensive: malformed events with no usable timestamps sort last (epoch fallback)."""
    pods_api.read_namespaced_pod.return_value = _detailed_pod()

    real_event = _event(reason="Real", age_minutes=5)
    malformed = SimpleNamespace(
        type="Normal",
        reason="Malformed",
        message="...",
        count=1,
        last_timestamp=None,
        event_time=None,
        first_timestamp=None,
        metadata=None,
    )
    pods_api.list_namespaced_event.return_value = SimpleNamespace(items=[malformed, real_event])

    result = await get_pod(
        GetPodInput(name="x", namespace="staging"), ctx=kube_context, settings=Settings()
    )

    reasons = [e["reason"] for e in result.data["events"]]
    assert reasons == ["Real", "Malformed"]
