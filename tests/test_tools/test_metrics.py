"""Tests for the ``top_pods`` tool."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools.metrics import (
    TopNodesInput,
    TopPodsInput,
    top_nodes,
    top_pods,
)

PATCH_TARGET = "k8s_mcp_server.tools.metrics"


def _pod_metric(
    name: str,
    *,
    namespace: str = "dev",
    containers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a dict-shaped pod metrics entry as returned by CustomObjectsApi."""
    return {
        "metadata": {"name": name, "namespace": namespace},
        "containers": containers
        if containers is not None
        else [{"name": "app", "usage": {"cpu": "100m", "memory": "128Mi"}}],
    }


@pytest.fixture
def metrics_api(patch_custom_objects: Callable[[str], MagicMock]) -> MagicMock:
    return patch_custom_objects(PATCH_TARGET)


# ---------------------------------------------------------------------------
# Namespace dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specific_namespace_calls_list_namespaced_custom_object(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [_pod_metric("api", namespace="dev")]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is True
    metrics_api.list_namespaced_custom_object.assert_called_once()
    kwargs = metrics_api.list_namespaced_custom_object.call_args.kwargs
    assert kwargs["group"] == "metrics.k8s.io"
    assert kwargs["version"] == "v1beta1"
    assert kwargs["plural"] == "pods"
    assert kwargs["namespace"] == "dev"
    metrics_api.list_cluster_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_none_uses_context_default(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {"items": []}

    await top_pods(TopPodsInput(), ctx=kube_context, settings=Settings())

    assert metrics_api.list_namespaced_custom_object.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_all_no_allowlist_calls_list_cluster_custom_object(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_cluster_custom_object.return_value = {
        "items": [_pod_metric("api"), _pod_metric("web", namespace="prod")]
    }

    result = await top_pods(TopPodsInput(namespace="all"), ctx=kube_context, settings=Settings())

    assert result.success is True
    metrics_api.list_cluster_custom_object.assert_called_once()
    metrics_api.list_namespaced_custom_object.assert_not_called()


@pytest.mark.asyncio
async def test_all_with_allowlist_iterates_allowlisted_namespaces(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.side_effect = [
        {"items": [_pod_metric("dev-pod", namespace="dev")]},
        {"items": [_pod_metric("staging-pod", namespace="staging")]},
    ]

    result = await top_pods(
        TopPodsInput(namespace="all"),
        ctx=kube_context,
        settings=Settings(namespaces=("staging", "dev")),
    )

    assert result.success is True
    metrics_api.list_cluster_custom_object.assert_not_called()
    called_namespaces = [
        call.kwargs["namespace"]
        for call in metrics_api.list_namespaced_custom_object.call_args_list
    ]
    assert called_namespaces == ["dev", "staging"]
    names = sorted(p["name"] for p in result.data["pods"])
    assert names == ["dev-pod", "staging-pod"]


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    result = await top_pods(
        TopPodsInput(namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")


@pytest.mark.asyncio
async def test_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    result = await top_pods(
        TopPodsInput(),
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


# ---------------------------------------------------------------------------
# Metrics-server missing (CRITICAL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_server_not_available_returns_friendly_error(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    """A 404 from the metrics API means metrics-server isn't installed.

    Pin the exact friendly error so the spec contract is testable.
    """
    metrics_api.list_namespaced_custom_object.side_effect = ApiException(
        status=404,
        reason="Not Found",
    )

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert result.error == "metrics-server not available"


@pytest.mark.asyncio
async def test_metrics_server_not_available_on_cluster_wide_query(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    """Same 404-as-metrics-server-missing handling on the cluster-wide path."""
    metrics_api.list_cluster_custom_object.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    result = await top_pods(TopPodsInput(namespace="all"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert result.error == "metrics-server not available"


# ---------------------------------------------------------------------------
# Quantity parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cpu_string,expected_millicores",
    [
        ("100m", 100),
        ("500m", 500),
        ("1", 1000),
        ("2", 2000),
        ("100000000n", 100),  # nanocores
        ("100u", 0),  # microcores rounds DOWN to 0 — matches kubectl top
        ("1m", 1),
    ],
)
@pytest.mark.asyncio
async def test_cpu_quantity_parsed_to_millicores(
    cpu_string: str,
    expected_millicores: int,
    kube_context: KubeContext,
    metrics_api: MagicMock,
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                "p",
                containers=[{"name": "c", "usage": {"cpu": cpu_string, "memory": "0"}}],
            )
        ]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.data["pods"][0]["cpu_millicores"] == expected_millicores


@pytest.mark.parametrize(
    "memory_string,expected_mib",
    [
        ("1Gi", 1024),
        ("1024Mi", 1024),
        ("512Mi", 512),
        ("1024Ki", 1),
        ("1023Ki", 0),  # sub-MiB rounds DOWN to 0 — matches kubectl top
        ("128Mi", 128),
    ],
)
@pytest.mark.asyncio
async def test_memory_quantity_parsed_to_mib(
    memory_string: str,
    expected_mib: int,
    kube_context: KubeContext,
    metrics_api: MagicMock,
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                "p",
                containers=[{"name": "c", "usage": {"cpu": "0", "memory": memory_string}}],
            )
        ]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.data["pods"][0]["memory_mib"] == expected_mib


@pytest.mark.asyncio
async def test_missing_cpu_value_treated_as_zero(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [_pod_metric("p", containers=[{"name": "c", "usage": {"memory": "128Mi"}}])]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.data["pods"][0]["cpu_millicores"] == 0
    assert result.data["pods"][0]["memory_mib"] == 128


@pytest.mark.asyncio
async def test_missing_memory_value_treated_as_zero(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [_pod_metric("p", containers=[{"name": "c", "usage": {"cpu": "200m"}}])]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.data["pods"][0]["cpu_millicores"] == 200
    assert result.data["pods"][0]["memory_mib"] == 0


@pytest.mark.asyncio
async def test_unparseable_quantity_treated_as_zero(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    """Defensive: a malformed Quantity string doesn't crash the tool."""
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                "p",
                containers=[
                    {
                        "name": "c",
                        "usage": {"cpu": "not-a-number", "memory": "garbage"},
                    }
                ],
            )
        ]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.data["pods"][0]["cpu_millicores"] == 0
    assert result.data["pods"][0]["memory_mib"] == 0


@pytest.mark.asyncio
async def test_missing_usage_dict_treated_as_zero(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    """Defensive: a container with no `usage` field at all."""
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [_pod_metric("p", containers=[{"name": "c"}])]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.data["pods"][0]["cpu_millicores"] == 0
    assert result.data["pods"][0]["memory_mib"] == 0


# ---------------------------------------------------------------------------
# Format / aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pod_level_numbers_sum_container_usage(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                "multi",
                containers=[
                    {"name": "app", "usage": {"cpu": "200m", "memory": "256Mi"}},
                    {"name": "sidecar", "usage": {"cpu": "50m", "memory": "128Mi"}},
                ],
            )
        ]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    out = result.data["pods"][0]
    assert out["cpu_millicores"] == 250
    assert out["memory_mib"] == 384
    assert len(out["containers"]) == 2
    assert out["containers"][0] == {"name": "app", "cpu_millicores": 200, "memory_mib": 256}
    assert out["containers"][1] == {"name": "sidecar", "cpu_millicores": 50, "memory_mib": 128}


@pytest.mark.asyncio
async def test_pod_with_no_containers_returns_zeros(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [_pod_metric("empty", containers=[])]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    out = result.data["pods"][0]
    assert out["cpu_millicores"] == 0
    assert out["memory_mib"] == 0
    assert out["containers"] == []


@pytest.mark.asyncio
async def test_missing_metadata_returns_unknown(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    """Defensive: items[i] with missing metadata block."""
    metrics_api.list_namespaced_custom_object.return_value = {"items": [{"containers": []}]}

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    out = result.data["pods"][0]
    assert out["name"] == "Unknown"
    assert out["namespace"] == "Unknown"


@pytest.mark.asyncio
async def test_empty_items_returns_empty_pods_list(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    """A 200 + empty items means metrics-server is up but no pods reported."""
    metrics_api.list_namespaced_custom_object.return_value = {"items": []}

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.data == {"pods": [], "truncated": False}


@pytest.mark.asyncio
async def test_items_missing_or_none_returns_empty_pods_list(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    """Defensive: API response with no ``items`` key OR explicit None."""
    metrics_api.list_namespaced_custom_object.return_value = {"items": None}

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.data == {"pods": [], "truncated": False}


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sorts_by_cpu_descending_by_default(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                "low", containers=[{"name": "c", "usage": {"cpu": "10m", "memory": "1Gi"}}]
            ),
            _pod_metric(
                "high",
                containers=[{"name": "c", "usage": {"cpu": "500m", "memory": "128Mi"}}],
            ),
            _pod_metric(
                "mid",
                containers=[{"name": "c", "usage": {"cpu": "200m", "memory": "256Mi"}}],
            ),
        ]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert [p["name"] for p in result.data["pods"]] == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_sorts_by_memory_descending_when_requested(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                "cpu-heavy",
                containers=[{"name": "c", "usage": {"cpu": "500m", "memory": "64Mi"}}],
            ),
            _pod_metric(
                "mem-heavy",
                containers=[{"name": "c", "usage": {"cpu": "50m", "memory": "1Gi"}}],
            ),
        ]
    }

    result = await top_pods(
        TopPodsInput(namespace="dev", sort_by="memory"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert [p["name"] for p in result.data["pods"]] == ["mem-heavy", "cpu-heavy"]


@pytest.mark.asyncio
async def test_name_breaks_ties_when_usage_equal(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                "zeta",
                containers=[{"name": "c", "usage": {"cpu": "100m", "memory": "128Mi"}}],
            ),
            _pod_metric(
                "alpha",
                containers=[{"name": "c", "usage": {"cpu": "100m", "memory": "128Mi"}}],
            ),
            _pod_metric(
                "mike",
                containers=[{"name": "c", "usage": {"cpu": "100m", "memory": "128Mi"}}],
            ),
        ]
    }

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert [p["name"] for p in result.data["pods"]] == ["alpha", "mike", "zeta"]


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_true_when_exceeds_limit(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {
        "items": [
            _pod_metric(
                f"p{i:02d}",
                containers=[{"name": "c", "usage": {"cpu": f"{i + 1}m", "memory": "1Mi"}}],
            )
            for i in range(8)
        ]
    }

    result = await top_pods(
        TopPodsInput(namespace="dev", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is True
    assert len(result.data["pods"]) == 5


@pytest.mark.asyncio
async def test_truncated_false_when_under_limit(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.return_value = {"items": [_pod_metric("only")]}

    result = await top_pods(
        TopPodsInput(namespace="dev", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is False
    assert len(result.data["pods"]) == 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_404_api_error_returns_kubernetes_api_error(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.side_effect = ApiException(
        status=500, reason="Internal"
    )

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")
    # Critical: must NOT be the metrics-server-missing message
    assert result.error != "metrics-server not available"


@pytest.mark.asyncio
async def test_unexpected_exception_returns_error(
    kube_context: KubeContext, metrics_api: MagicMock
) -> None:
    metrics_api.list_namespaced_custom_object.side_effect = RuntimeError("boom")

    result = await top_pods(TopPodsInput(namespace="dev"), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": "nope"},
        {"sort_by": "rss"},  # not in Literal
        {"sort_by": "CPU"},  # case-sensitive
        {"limit": 0},
        {"limit": 201},  # over max
        {"limit": -1},
    ],
)
def test_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TopPodsInput.model_validate(payload)


@pytest.mark.parametrize("sort_by", ["cpu", "memory"])
def test_input_accepts_both_sort_by_values(sort_by: str) -> None:
    inp = TopPodsInput.model_validate({"sort_by": sort_by})
    assert inp.sort_by == sort_by


# ===========================================================================
# top_nodes
# ===========================================================================


def _node_metric(name: str, *, cpu: str = "100m", memory: str = "128Mi") -> dict[str, Any]:
    """Build a dict-shaped node metrics entry as returned by CustomObjectsApi."""
    return {
        "metadata": {"name": name},
        "usage": {"cpu": cpu, "memory": memory},
    }


def _node_alloc(name: str, *, cpu: str = "2", memory: str = "4Gi") -> SimpleNamespace:
    """Build a V1Node-like object exposing allocatable cpu/memory for list_node."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(allocatable={"cpu": cpu, "memory": memory}),
    )


@pytest.fixture
def top_nodes_apis(
    patch_custom_objects: Callable[[str], MagicMock],
    patch_core_v1: Callable[[str], MagicMock],
) -> SimpleNamespace:
    """Both APIs patched inside the metrics module: CustomObjectsApi for the
    metrics fetch + CoreV1Api for the allocatable map fetch."""
    return SimpleNamespace(
        metrics=patch_custom_objects(PATCH_TARGET),
        core=patch_core_v1(PATCH_TARGET),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_nodes_returns_usage_and_percent(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("node-1", cpu="1500m", memory="2Gi")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(
        items=[_node_alloc("node-1", cpu="2", memory="4Gi")]
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    out = result.data["nodes"][0]
    assert out["name"] == "node-1"
    assert out["cpu_millicores"] == 1500
    assert out["memory_mib"] == 2048
    assert out["cpu_percent"] == 75  # 1500 / 2000
    assert out["memory_percent"] == 50  # 2048 / 4096


@pytest.mark.asyncio
async def test_top_nodes_calls_cluster_custom_object_with_nodes_plural(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {"items": []}
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    kwargs = top_nodes_apis.metrics.list_cluster_custom_object.call_args.kwargs
    assert kwargs["group"] == "metrics.k8s.io"
    assert kwargs["version"] == "v1beta1"
    assert kwargs["plural"] == "nodes"


# ---------------------------------------------------------------------------
# Percent calculation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cpu_percent_rounded_to_int(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """1234m of 2000m = 61.7% → round() → 62"""
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("n", cpu="1234m", memory="0")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(
        items=[_node_alloc("n", cpu="2", memory="4Gi")]
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data["nodes"][0]["cpu_percent"] == 62


@pytest.mark.asyncio
async def test_overcommit_percent_above_100_surfaced_not_clamped(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """Usage > allocatable is a real diagnostic signal; we don't clamp."""
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("hot", cpu="2500m", memory="5Gi")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(
        items=[_node_alloc("hot", cpu="2", memory="4Gi")]
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["cpu_percent"] == 125  # NOT 100
    assert out["memory_percent"] == 125  # NOT 100


# ---------------------------------------------------------------------------
# Partial-success: percent fields null when allocatable unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cpu_percent_none_when_allocatable_cpu_zero(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """allocatable.cpu='0' (decommissioned node) → cpu_percent=None, no zero-div."""
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("n", cpu="100m", memory="128Mi")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(
        items=[_node_alloc("n", cpu="0", memory="4Gi")]
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["cpu_percent"] is None
    assert out["memory_percent"] is not None  # memory still populated


@pytest.mark.asyncio
async def test_memory_percent_none_when_allocatable_memory_missing(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """allocatable.memory unparseable → memory_percent=None, cpu_percent populated."""
    n = _node_alloc("n", cpu="2", memory="garbage")
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("n", cpu="500m", memory="128Mi")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[n])

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["cpu_percent"] is not None
    assert out["memory_percent"] is None


@pytest.mark.asyncio
async def test_both_percents_none_when_node_not_in_allocatable_map(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """Metrics returns 'ghost' but list_node returns only 'other' (skew)."""
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("ghost", cpu="100m", memory="128Mi")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[_node_alloc("other")])

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["name"] == "ghost"
    assert out["cpu_percent"] is None
    assert out["memory_percent"] is None
    # Usage values still populate — only the percent calc fails
    assert out["cpu_millicores"] == 100
    assert out["memory_mib"] == 128


@pytest.mark.asyncio
async def test_all_percents_none_when_list_node_fails(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """list_node failure must NOT fail the whole tool — usage data still returns."""
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [
            _node_metric("a", cpu="100m", memory="128Mi"),
            _node_metric("b", cpu="200m", memory="256Mi"),
        ]
    }
    top_nodes_apis.core.list_node.side_effect = ApiException(status=403, reason="Forbidden")

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.error is None
    for node in result.data["nodes"]:
        assert node["cpu_percent"] is None
        assert node["memory_percent"] is None
        # Usage still surfaces
        assert node["cpu_millicores"] > 0


@pytest.mark.asyncio
async def test_all_percents_none_when_list_node_unexpected_exception(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {"items": [_node_metric("n")]}
    top_nodes_apis.core.list_node.side_effect = RuntimeError("node fetch boom")

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.data["nodes"][0]["cpu_percent"] is None
    assert result.data["nodes"][0]["memory_percent"] is None


@pytest.mark.asyncio
async def test_allocatable_map_skips_nodes_with_missing_metadata_name(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """Defensive: V1Node items with no metadata.name don't crash the map build."""
    nameless = SimpleNamespace(
        metadata=SimpleNamespace(name=None),
        status=SimpleNamespace(allocatable={"cpu": "2", "memory": "4Gi"}),
    )
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("real", cpu="500m", memory="2Gi")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(
        items=[nameless, _node_alloc("real", cpu="2", memory="4Gi")]
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["name"] == "real"
    assert out["cpu_percent"] == 25


# ---------------------------------------------------------------------------
# metrics-server missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_nodes_metrics_server_not_available_returns_friendly_error(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """404 from the node metrics endpoint pins the exact friendly phrase."""
    top_nodes_apis.metrics.list_cluster_custom_object.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert result.error == "metrics-server not available"
    top_nodes_apis.core.list_node.assert_not_called()


# ---------------------------------------------------------------------------
# Sort, truncation, defensive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_nodes_sorts_by_cpu_descending_default(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [
            _node_metric("low", cpu="10m", memory="2Gi"),
            _node_metric("high", cpu="1500m", memory="128Mi"),
            _node_metric("mid", cpu="500m", memory="256Mi"),
        ]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert [n["name"] for n in result.data["nodes"]] == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_top_nodes_sorts_by_memory_descending_when_requested(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [
            _node_metric("cpu-heavy", cpu="2", memory="64Mi"),
            _node_metric("mem-heavy", cpu="100m", memory="4Gi"),
        ]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(sort_by="memory"), ctx=kube_context, settings=Settings())

    assert [n["name"] for n in result.data["nodes"]] == ["mem-heavy", "cpu-heavy"]


@pytest.mark.asyncio
async def test_top_nodes_name_breaks_ties_when_usage_equal(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [
            _node_metric("zeta", cpu="500m", memory="1Gi"),
            _node_metric("alpha", cpu="500m", memory="1Gi"),
            _node_metric("mike", cpu="500m", memory="1Gi"),
        ]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert [n["name"] for n in result.data["nodes"]] == ["alpha", "mike", "zeta"]


@pytest.mark.asyncio
async def test_top_nodes_truncated_true_when_exceeds_limit(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric(f"n{i:02d}", cpu=f"{i + 1}m", memory="1Mi") for i in range(8)]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(limit=5), ctx=kube_context, settings=Settings())

    assert result.data["truncated"] is True
    assert len(result.data["nodes"]) == 5


@pytest.mark.asyncio
async def test_top_nodes_truncated_false_when_under_limit(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("only")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(limit=5), ctx=kube_context, settings=Settings())

    assert result.data["truncated"] is False
    assert len(result.data["nodes"]) == 1


@pytest.mark.asyncio
async def test_top_nodes_empty_items_returns_empty_list(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {"items": []}
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    assert result.data == {"nodes": [], "truncated": False}


@pytest.mark.asyncio
async def test_top_nodes_items_none_returns_empty_list(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {"items": None}
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.data == {"nodes": [], "truncated": False}


@pytest.mark.asyncio
async def test_top_nodes_missing_metadata_or_usage_does_not_crash(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [{}]  # neither metadata nor usage
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(items=[])

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    out = result.data["nodes"][0]
    assert out["name"] == "Unknown"
    assert out["cpu_millicores"] == 0
    assert out["memory_mib"] == 0
    assert out["cpu_percent"] is None
    assert out["memory_percent"] is None


@pytest.mark.asyncio
async def test_top_nodes_allocatable_with_missing_status(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    """Defensive: V1Node with status=None → empty allocatable in map."""
    top_nodes_apis.metrics.list_cluster_custom_object.return_value = {
        "items": [_node_metric("n", cpu="500m", memory="2Gi")]
    }
    top_nodes_apis.core.list_node.return_value = SimpleNamespace(
        items=[SimpleNamespace(metadata=SimpleNamespace(name="n"), status=None)]
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    out = result.data["nodes"][0]
    assert out["cpu_percent"] is None
    assert out["memory_percent"] is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_nodes_non_404_api_error_returns_kubernetes_api_error(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.side_effect = ApiException(
        status=500, reason="Internal"
    )

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")
    # Critical: must NOT be the metrics-server-missing message
    assert result.error != "metrics-server not available"


@pytest.mark.asyncio
async def test_top_nodes_unexpected_exception_returns_error(
    kube_context: KubeContext, top_nodes_apis: SimpleNamespace
) -> None:
    top_nodes_apis.metrics.list_cluster_custom_object.side_effect = RuntimeError("boom")

    result = await top_nodes(TopNodesInput(), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_top_nodes_input_rejects_namespace_field() -> None:
    """Nodes are cluster-scoped — namespace field is a ValidationError."""
    with pytest.raises(ValidationError):
        TopNodesInput.model_validate({"namespace": "dev"})


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": "nope"},
        {"sort_by": "rss"},
        {"sort_by": "CPU"},
        {"limit": 0},
        {"limit": 101},  # over max (100, smaller than top_pods's 200)
        {"limit": -1},
    ],
)
def test_top_nodes_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TopNodesInput.model_validate(payload)
