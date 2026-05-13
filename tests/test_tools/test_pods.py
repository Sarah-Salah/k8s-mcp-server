"""Tests for the ``list_pods`` tool."""

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
from k8s_mcp_server.tools.pods import ListPodsInput, list_pods

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
