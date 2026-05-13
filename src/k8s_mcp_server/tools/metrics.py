"""Metrics tools: ``top_pods`` and ``top_nodes`` (metrics.k8s.io)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from kubernetes.client import CoreV1Api, CustomObjectsApi
from kubernetes.client.exceptions import ApiException
from kubernetes.utils import parse_quantity
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import NamespaceNotAllowedError, resolve_read_namespaces
from k8s_mcp_server.tools._registry import ToolResult, register_tool

logger = logging.getLogger(__name__)

_METRICS_API_GROUP = "metrics.k8s.io"
_METRICS_API_VERSION = "v1beta1"
_METRICS_API_PLURAL_PODS = "pods"
_METRICS_API_PLURAL_NODES = "nodes"
_MIB = 1024 * 1024


class TopPodsInput(BaseModel):
    """Inputs for ``top_pods``."""

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    sort_by: Literal["cpu", "memory"] = "cpu"
    limit: int = Field(default=20, ge=1, le=200)


@register_tool(
    name="top_pods",
    description=(
        "Pod resource usage (CPU, memory) from the metrics.k8s.io API. "
        "Requires metrics-server installed in the cluster. Returns "
        "per-pod totals plus per-container breakdown. Sorted by sort_by "
        "(cpu or memory) descending; ties broken by name. namespace='all' "
        "queries cluster-wide (or the configured allowlist)."
    ),
    input_model=TopPodsInput,
)
async def top_pods(
    inp: TopPodsInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Pod resource usage. Requires metrics-server installed in the cluster."""
    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    api = CustomObjectsApi(ctx.api_client)
    try:
        items = await _fetch_pod_metrics(api, targets)
    except ApiException as exc:
        if exc.status == 404:
            # A 404 from the metrics.k8s.io list endpoint means the API itself
            # isn't registered (i.e., metrics-server isn't installed). A
            # successful list on a cluster with no pods returns 200 + empty items.
            return ToolResult(success=False, error="metrics-server not available")
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("top_pods failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    pods = [_format_pod_metrics(it) for it in items]
    sort_field = "cpu_millicores" if inp.sort_by == "cpu" else "memory_mib"
    pods.sort(key=lambda p: (-p[sort_field], p["name"] or ""))
    truncated = len(pods) > inp.limit
    return ToolResult(
        success=True,
        data={"pods": pods[: inp.limit], "truncated": truncated},
    )


async def _fetch_pod_metrics(
    api: CustomObjectsApi, targets: list[str] | None
) -> list[dict[str, Any]]:
    if targets is None:
        res = await asyncio.to_thread(
            api.list_cluster_custom_object,
            group=_METRICS_API_GROUP,
            version=_METRICS_API_VERSION,
            plural=_METRICS_API_PLURAL_PODS,
        )
        return list(res.get("items") or [])

    collected: list[dict[str, Any]] = []
    for ns in targets:
        res = await asyncio.to_thread(
            api.list_namespaced_custom_object,
            group=_METRICS_API_GROUP,
            version=_METRICS_API_VERSION,
            plural=_METRICS_API_PLURAL_PODS,
            namespace=ns,
        )
        collected.extend(res.get("items") or [])
    return collected


def _format_pod_metrics(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") or {}
    containers_raw = item.get("containers") or []
    containers = [
        {
            "name": c.get("name"),
            "cpu_millicores": _cpu_to_millicores((c.get("usage") or {}).get("cpu")),
            "memory_mib": _memory_to_mib((c.get("usage") or {}).get("memory")),
        }
        for c in containers_raw
    ]
    return {
        "name": metadata.get("name") or "Unknown",
        "namespace": metadata.get("namespace") or "Unknown",
        "cpu_millicores": sum(c["cpu_millicores"] for c in containers),
        "memory_mib": sum(c["memory_mib"] for c in containers),
        "containers": containers,
    }


def _cpu_to_millicores(value: str | None) -> int:
    """Parse a K8s CPU Quantity string to integer millicores.

    ``parse_quantity`` returns ``Decimal`` cores; multiply by 1000 then
    truncate via ``int()``. Matches kubectl top semantics — sub-millicore
    CPU displays as 0 (intentional truncation, do not change to ceiling).
    Missing or unparseable input → 0 so a malformed container doesn't error
    the whole pod.
    """
    if not value:
        return 0
    try:
        cores = parse_quantity(value)
    except Exception:
        return 0
    return int(cores * 1000)


def _memory_to_mib(value: str | None) -> int:
    """Parse a K8s memory Quantity string to integer MiB.

    ``parse_quantity`` returns ``Decimal`` bytes; divide by 1024*1024 then
    truncate via ``int()``. Matches kubectl top semantics — sub-MiB memory
    displays as 0 (intentional truncation). Missing or unparseable input → 0.
    """
    if not value:
        return 0
    try:
        bytes_ = parse_quantity(value)
    except Exception:
        return 0
    return int(bytes_ / _MIB)


# ===========================================================================
# top_nodes
# ===========================================================================


class TopNodesInput(BaseModel):
    """Inputs for ``top_nodes``.

    No ``namespace`` field — nodes are cluster-scoped resources.
    """

    model_config = ConfigDict(extra="forbid")

    sort_by: Literal["cpu", "memory"] = "cpu"
    limit: int = Field(default=20, ge=1, le=100)


@register_tool(
    name="top_nodes",
    description=(
        "Node resource usage (CPU, memory) from the metrics.k8s.io API, "
        "plus percent utilisation against each node's allocatable capacity. "
        "Cluster-scoped — no namespace input. Requires metrics-server "
        "installed. cpu_percent / memory_percent are null when the node's "
        "allocatable capacity can't be fetched (partial-success on the "
        "underlying list_node call). Sorted by sort_by descending; ties "
        "broken by name."
    ),
    input_model=TopNodesInput,
)
async def top_nodes(
    inp: TopNodesInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Node resource usage. Requires metrics-server installed in the cluster."""
    del settings  # nodes are cluster-scoped; no allowlist applies

    metrics_api = CustomObjectsApi(ctx.api_client)
    try:
        items = await _fetch_node_metrics(metrics_api)
    except ApiException as exc:
        if exc.status == 404:
            return ToolResult(success=False, error="metrics-server not available")
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("top_nodes failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    # Single batch fetch — 1 API call, not N. Partial-success: empty map on
    # failure means every node's percent fields surface as None.
    core_api = CoreV1Api(ctx.api_client)
    allocatable_map = await _fetch_node_allocatable_map(core_api)

    nodes = [_format_node_metrics(it, allocatable_map) for it in items]
    sort_field = "cpu_millicores" if inp.sort_by == "cpu" else "memory_mib"
    nodes.sort(key=lambda n: (-n[sort_field], n["name"] or ""))
    truncated = len(nodes) > inp.limit
    return ToolResult(
        success=True,
        data={"nodes": nodes[: inp.limit], "truncated": truncated},
    )


async def _fetch_node_metrics(api: CustomObjectsApi) -> list[dict[str, Any]]:
    res = await asyncio.to_thread(
        api.list_cluster_custom_object,
        group=_METRICS_API_GROUP,
        version=_METRICS_API_VERSION,
        plural=_METRICS_API_PLURAL_NODES,
    )
    return list(res.get("items") or [])


async def _fetch_node_allocatable_map(api: CoreV1Api) -> dict[str, dict[str, int]]:
    """Build ``{node_name: {cpu_millicores, mem_mib}}`` from a single list_node call.

    Partial-success: a failure here returns an empty dict (with a warning
    logged), and the caller surfaces every node's percent fields as None.
    One batch API call for the whole cluster — not N reads — keeps this
    cheap even on clusters with hundreds of nodes.
    """
    try:
        result = await asyncio.to_thread(api.list_node)
    except ApiException:
        logger.warning("top_nodes: failed to fetch node allocatable map")
        return {}
    except Exception:
        logger.exception("top_nodes: unexpected error fetching node allocatable map")
        return {}

    mapping: dict[str, dict[str, int]] = {}
    for n in result.items:
        metadata = getattr(n, "metadata", None)
        name = getattr(metadata, "name", None) if metadata else None
        if not name:
            continue
        status = getattr(n, "status", None)
        alloc = (getattr(status, "allocatable", None) or {}) if status else {}
        mapping[name] = {
            "cpu_millicores": _cpu_to_millicores(alloc.get("cpu")),
            "mem_mib": _memory_to_mib(alloc.get("memory")),
        }
    return mapping


def _format_node_metrics(
    item: dict[str, Any], alloc_map: dict[str, dict[str, int]]
) -> dict[str, Any]:
    metadata = item.get("metadata") or {}
    usage = item.get("usage") or {}
    name = metadata.get("name") or "Unknown"
    cpu_m = _cpu_to_millicores(usage.get("cpu"))
    mem_m = _memory_to_mib(usage.get("memory"))

    alloc = alloc_map.get(name) or {}
    cpu_alloc = alloc.get("cpu_millicores", 0)
    mem_alloc = alloc.get("mem_mib", 0)

    # Overcommit (usage > allocatable) yields percent > 100. We surface this
    # as-is rather than clamping at 100, because exceeding allocatable is a
    # real diagnostic signal in production (pods over their requests/limits,
    # eviction risk). Clamping would hide it from the LLM.
    return {
        "name": name,
        "cpu_millicores": cpu_m,
        "memory_mib": mem_m,
        "cpu_percent": round(cpu_m * 100 / cpu_alloc) if cpu_alloc else None,
        "memory_percent": round(mem_m * 100 / mem_alloc) if mem_alloc else None,
    }
