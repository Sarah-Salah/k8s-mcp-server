"""Tests for the ``top_pods`` tool."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools.metrics import TopPodsInput, top_pods

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
