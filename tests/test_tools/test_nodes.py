"""Tests for the ``list_nodes`` tool."""

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
from k8s_mcp_server.tools.nodes import (
    GetNodeInput,
    ListNodesInput,
    get_node,
    list_nodes,
)

PATCH_TARGET = "k8s_mcp_server.tools.nodes"


def _condition(type_: str, status: str) -> SimpleNamespace:
    return SimpleNamespace(type=type_, status=status)


def _node(
    name: str,
    *,
    ready: str | None = "True",
    extra_conditions: list[SimpleNamespace] | None = None,
    labels: dict[str, str] | None = None,
    age_minutes: int = 60,
    kubelet_version: str | None = "v1.28.3",
    capacity: dict[str, str] | None = None,
    allocatable: dict[str, str] | None = None,
) -> SimpleNamespace:
    conditions: list[SimpleNamespace] = list(extra_conditions or [])
    if ready is not None:
        conditions.append(_condition("Ready", ready))
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            creation_timestamp=created,
            labels=labels if labels is not None else {},
        ),
        status=SimpleNamespace(
            conditions=conditions,
            node_info=SimpleNamespace(kubelet_version=kubelet_version),
            capacity=capacity if capacity is not None else {"cpu": "4", "memory": "16Gi"},
            allocatable=(
                allocatable if allocatable is not None else {"cpu": "3800m", "memory": "15Gi"}
            ),
        ),
    )


@pytest.fixture
def nodes_api(patch_core_v1: Callable[[str], MagicMock]) -> MagicMock:
    return patch_core_v1(PATCH_TARGET)


# ---------------------------------------------------------------------------
# Happy path / sorting / age
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_nodes_returns_all_nodes(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[_node("node-1"), _node("node-2")])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert [n["name"] for n in result.data["nodes"]] == ["node-1", "node-2"]


@pytest.mark.asyncio
async def test_sorted_by_name(kube_context: KubeContext, nodes_api: MagicMock) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[_node("zeta"), _node("alpha"), _node("mike")]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert [n["name"] for n in result.data["nodes"]] == ["alpha", "mike", "zeta"]


@pytest.mark.asyncio
async def test_includes_age_human(kube_context: KubeContext, nodes_api: MagicMock) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[_node("node-1", age_minutes=180)])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["age_seconds"] >= 180 * 60
    assert "h" in out["age_human"]


# ---------------------------------------------------------------------------
# Label selector / truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_selector_passed_to_api(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[])

    await list_nodes(
        ListNodesInput(label_selector="role=worker,zone=us-east-1a"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert nodes_api.list_node.call_args.kwargs["label_selector"] == "role=worker,zone=us-east-1a"


@pytest.mark.asyncio
async def test_label_selector_is_none_when_not_set(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[])

    await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert nodes_api.list_node.call_args.kwargs["label_selector"] is None


@pytest.mark.asyncio
async def test_truncated_true_when_exceeds_limit(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[_node(f"node-{i:02d}") for i in range(8)]
    )

    result = await list_nodes(ListNodesInput(limit=5), ctx=kube_context, settings=Settings())

    assert result.data["truncated"] is True
    assert len(result.data["nodes"]) == 5


@pytest.mark.asyncio
async def test_truncated_false_when_under_limit(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[_node("only")])

    result = await list_nodes(ListNodesInput(limit=5), ctx=kube_context, settings=Settings())

    assert result.data["truncated"] is False
    assert len(result.data["nodes"]) == 1


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_ready_when_condition_true(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[_node("n", ready="True")])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["status"] == "Ready"


@pytest.mark.asyncio
async def test_status_notready_when_condition_false(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[_node("n", ready="False")])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["status"] == "NotReady"


@pytest.mark.asyncio
async def test_status_unknown_when_condition_unknown(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    """Kubelet stale: Ready condition with status='Unknown' (e.g., node has stopped reporting)."""
    nodes_api.list_node.return_value = SimpleNamespace(items=[_node("n", ready="Unknown")])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["status"] == "Unknown"


@pytest.mark.asyncio
async def test_status_unknown_when_no_ready_condition(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    """Node with some conditions but no Ready type — defaults to Unknown."""
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[
            _node(
                "n",
                ready=None,
                extra_conditions=[_condition("MemoryPressure", "False")],
            )
        ]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["status"] == "Unknown"


@pytest.mark.asyncio
async def test_status_unknown_when_no_conditions_at_all(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(items=[_node("n", ready=None)])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["status"] == "Unknown"


# ---------------------------------------------------------------------------
# Role derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roles_from_node_role_labels(kube_context: KubeContext, nodes_api: MagicMock) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[_node("cp", labels={"node-role.kubernetes.io/control-plane": ""})]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["roles"] == ["control-plane"]


@pytest.mark.asyncio
async def test_roles_sorted_when_multiple(kube_context: KubeContext, nodes_api: MagicMock) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[
            _node(
                "mixed",
                labels={
                    "node-role.kubernetes.io/master": "",
                    "node-role.kubernetes.io/control-plane": "",
                    "other": "ignored",
                },
            )
        ]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["roles"] == ["control-plane", "master"]


@pytest.mark.asyncio
async def test_roles_defaults_to_worker_when_no_role_labels(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    """Common in managed clusters (GKE/EKS) where worker nodes have no role label."""
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[_node("worker", labels={"kubernetes.io/arch": "amd64"})]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["roles"] == ["worker"]


@pytest.mark.asyncio
async def test_roles_ignores_empty_suffix_label(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    """Defensive: a bare 'node-role.kubernetes.io/' label (no role suffix) is skipped."""
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[
            _node(
                "weird",
                labels={"node-role.kubernetes.io/": "", "node-role.kubernetes.io/edge": ""},
            )
        ]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["roles"] == ["edge"]


# ---------------------------------------------------------------------------
# Resource fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kubelet_version_passed_through(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[_node("n", kubelet_version="v1.30.0")]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["kubelet_version"] == "v1.30.0"


@pytest.mark.asyncio
async def test_capacity_and_allocatable_passed_through_as_strings(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.return_value = SimpleNamespace(
        items=[
            _node(
                "n",
                capacity={"cpu": "8", "memory": "32Gi", "pods": "110"},
                allocatable={"cpu": "7600m", "memory": "30Gi", "pods": "110"},
            )
        ]
    )

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["capacity"] == {"cpu": "8", "memory": "32Gi", "pods": "110"}
    assert out["allocatable"] == {"cpu": "7600m", "memory": "30Gi", "pods": "110"}


@pytest.mark.asyncio
async def test_capacity_and_allocatable_default_to_empty_dict_when_missing(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    n = _node("n")
    n.status.capacity = None
    n.status.allocatable = None
    nodes_api.list_node.return_value = SimpleNamespace(items=[n])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["capacity"] == {}
    assert out["allocatable"] == {}


# ---------------------------------------------------------------------------
# Defensive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_missing_metadata_status_does_not_crash(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    weird: Any = SimpleNamespace(metadata=None, status=None)
    nodes_api.list_node.return_value = SimpleNamespace(items=[weird])

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    [out] = result.data["nodes"]
    assert out["name"] == "Unknown"
    assert out["status"] == "Unknown"
    assert out["roles"] == ["worker"]
    assert out["kubelet_version"] is None
    assert out["capacity"] == {}
    assert out["allocatable"] == {}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_error_on_api_exception(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.side_effect = ApiException(status=403, reason="Forbidden")

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "Forbidden" in (result.error or "")


@pytest.mark.asyncio
async def test_returns_error_on_unexpected_exception(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.list_node.side_effect = RuntimeError("boom")

    result = await list_nodes(ListNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_input_rejects_namespace_field() -> None:
    """Nodes are cluster-scoped — passing a namespace field is a ValidationError."""
    with pytest.raises(ValidationError):
        ListNodesInput.model_validate({"namespace": "dev"})


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": "nope"},
        {"limit": 0},
        {"limit": -1},
        {"limit": 1001},
    ],
)
def test_input_rejects_invalid_payloads(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ListNodesInput.model_validate(payload)


# ===========================================================================
# get_node
# ===========================================================================


def _taint(key: str, *, value: str | None = None, effect: str = "NoSchedule") -> SimpleNamespace:
    return SimpleNamespace(key=key, value=value, effect=effect)


def _detailed_node(
    name: str = "node-1",
    *,
    ready: str | None = "True",
    ready_reason: str | None = "KubeletReady",
    ready_message: str | None = "kubelet is posting ready status",
    ready_age_minutes: int = 100,
    extra_conditions: list[SimpleNamespace] | None = None,
    labels: dict[str, str] | None = None,
    age_minutes: int = 120,
    kubelet_version: str | None = "v1.28.3",
    capacity: dict[str, str] | None = None,
    allocatable: dict[str, str] | None = None,
    taints: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    conditions: list[SimpleNamespace] = list(extra_conditions or [])
    if ready is not None:
        conditions.append(
            SimpleNamespace(
                type="Ready",
                status=ready,
                reason=ready_reason,
                message=ready_message,
                last_transition_time=datetime.now(UTC) - timedelta(minutes=ready_age_minutes),
            )
        )
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            creation_timestamp=created,
            labels=labels if labels is not None else {},
        ),
        spec=SimpleNamespace(taints=taints),
        status=SimpleNamespace(
            conditions=conditions,
            node_info=SimpleNamespace(kubelet_version=kubelet_version),
            capacity=capacity if capacity is not None else {"cpu": "4", "memory": "16Gi"},
            allocatable=(
                allocatable if allocatable is not None else {"cpu": "3800m", "memory": "15Gi"}
            ),
        ),
    )


@pytest.mark.asyncio
async def test_get_node_returns_full_state(kube_context: KubeContext, nodes_api: MagicMock) -> None:
    nodes_api.read_node.return_value = _detailed_node(
        name="node-1",
        labels={"node-role.kubernetes.io/worker": ""},
        capacity={"cpu": "8", "memory": "32Gi", "pods": "110"},
        allocatable={"cpu": "7600m", "memory": "30Gi", "pods": "110"},
        taints=[_taint("node.kubernetes.io/unreachable", effect="NoExecute")],
    )
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(
        items=[SimpleNamespace() for _ in range(27)]
    )

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.success is True
    data = result.data
    assert data["name"] == "node-1"
    assert data["status"] == "Ready"
    assert data["roles"] == ["worker"]
    assert data["age_seconds"] >= 120 * 60
    assert "h" in data["age_human"]
    assert data["kubelet_version"] == "v1.28.3"
    assert data["capacity"] == {"cpu": "8", "memory": "32Gi", "pods": "110"}
    assert data["allocatable"] == {"cpu": "7600m", "memory": "30Gi", "pods": "110"}
    assert len(data["conditions"]) == 1
    assert data["conditions"][0]["type"] == "Ready"
    assert data["conditions"][0]["reason"] == "KubeletReady"
    assert data["conditions"][0]["last_transition_age_seconds"] >= 100 * 60
    assert data["taints"] == [
        {"key": "node.kubernetes.io/unreachable", "value": None, "effect": "NoExecute"}
    ]
    assert data["pods_on_node"] == 27


@pytest.mark.asyncio
async def test_get_node_404_returns_friendly_not_found_error(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.read_node.side_effect = ApiException(status=404, reason="Not Found")

    result = await get_node(GetNodeInput(name="ghost-node"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert result.error == "node 'ghost-node' not found"
    nodes_api.list_pod_for_all_namespaces.assert_not_called()


@pytest.mark.asyncio
async def test_get_node_non_404_api_error_returns_kubernetes_api_error(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.read_node.side_effect = ApiException(status=500, reason="Internal")

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")


@pytest.mark.asyncio
async def test_get_node_unexpected_exception_returns_error(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.read_node.side_effect = RuntimeError("boom")

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_get_node_pod_count_field_selector_uses_node_name(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.read_node.return_value = _detailed_node(name="node-2")
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])

    await get_node(GetNodeInput(name="node-2"), ctx=kube_context, settings=Settings())

    kwargs = nodes_api.list_pod_for_all_namespaces.call_args.kwargs
    assert kwargs["field_selector"] == "spec.nodeName=node-2"
    assert kwargs["limit"] == 1000


@pytest.mark.asyncio
async def test_get_node_pods_on_node_count_returned(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.read_node.return_value = _detailed_node()
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(
        items=[SimpleNamespace() for _ in range(5)]
    )

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.data["pods_on_node"] == 5


@pytest.mark.asyncio
async def test_get_node_pod_count_fetch_failure_returns_null(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.read_node.return_value = _detailed_node(name="node-1")
    nodes_api.list_pod_for_all_namespaces.side_effect = ApiException(status=403, reason="Forbidden")

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.error is None
    assert result.data["pods_on_node"] is None
    assert result.data["name"] == "node-1"


@pytest.mark.asyncio
async def test_get_node_pod_count_unexpected_exception_returns_null(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    nodes_api.read_node.return_value = _detailed_node()
    nodes_api.list_pod_for_all_namespaces.side_effect = RuntimeError("pod count boom")

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.data["pods_on_node"] is None


@pytest.mark.asyncio
async def test_get_node_conditions_with_full_detail(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    """Uses shared format_condition — surfaces type/status/reason/message/age."""
    extra = SimpleNamespace(
        type="MemoryPressure",
        status="False",
        reason="KubeletHasSufficientMemory",
        message="kubelet has sufficient memory available",
        last_transition_time=datetime.now(UTC) - timedelta(minutes=300),
    )
    nodes_api.read_node.return_value = _detailed_node(extra_conditions=[extra])
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    types = {c["type"]: c for c in result.data["conditions"]}
    assert types["Ready"]["status"] == "True"
    assert types["MemoryPressure"]["status"] == "False"
    assert types["MemoryPressure"]["reason"] == "KubeletHasSufficientMemory"
    assert types["MemoryPressure"]["last_transition_age_seconds"] >= 300 * 60


@pytest.mark.asyncio
async def test_get_node_taints_formatted(kube_context: KubeContext, nodes_api: MagicMock) -> None:
    nodes_api.read_node.return_value = _detailed_node(
        taints=[
            _taint("dedicated", value="gpu", effect="NoSchedule"),
            _taint("node.kubernetes.io/unschedulable", effect="NoSchedule"),
        ]
    )
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.data["taints"] == [
        {"key": "dedicated", "value": "gpu", "effect": "NoSchedule"},
        {
            "key": "node.kubernetes.io/unschedulable",
            "value": None,
            "effect": "NoSchedule",
        },
    ]


@pytest.mark.asyncio
async def test_get_node_taints_default_to_empty_list_when_none(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    """spec.taints is None on untainted nodes."""
    nodes_api.read_node.return_value = _detailed_node(taints=None)
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.data["taints"] == []


@pytest.mark.asyncio
async def test_get_node_reuses_ready_status_and_roles_from_list_nodes_logic(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    """NotReady + multiple role labels → derived consistently with list_nodes."""
    nodes_api.read_node.return_value = _detailed_node(
        ready="False",
        labels={
            "node-role.kubernetes.io/control-plane": "",
            "node-role.kubernetes.io/etcd": "",
        },
    )
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])

    result = await get_node(GetNodeInput(name="node-1"), ctx=kube_context, settings=Settings())

    assert result.data["status"] == "NotReady"
    assert result.data["roles"] == ["control-plane", "etcd"]


@pytest.mark.asyncio
async def test_get_node_with_missing_metadata_spec_status_does_not_crash(
    kube_context: KubeContext, nodes_api: MagicMock
) -> None:
    weird: Any = SimpleNamespace(metadata=None, spec=None, status=None)
    nodes_api.read_node.return_value = weird
    nodes_api.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[])

    result = await get_node(GetNodeInput(name="x"), ctx=kube_context, settings=Settings())

    assert result.success is True
    data = result.data
    assert data["name"] == "Unknown"
    assert data["status"] == "Unknown"
    assert data["roles"] == ["worker"]
    assert data["kubelet_version"] is None
    assert data["capacity"] == {}
    assert data["allocatable"] == {}
    assert data["conditions"] == []
    assert data["taints"] == []
    assert data["pods_on_node"] == 0


def test_get_node_input_rejects_namespace_field() -> None:
    """Nodes are cluster-scoped — namespace field is a ValidationError."""
    with pytest.raises(ValidationError):
        GetNodeInput.model_validate({"name": "node-1", "namespace": "dev"})


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing name
        {"name": ""},  # empty name
        {"name": "x", "extra": "nope"},  # extra field
    ],
)
def test_get_node_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GetNodeInput.model_validate(payload)
