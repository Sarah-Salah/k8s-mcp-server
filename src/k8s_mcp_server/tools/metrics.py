"""``top_pods`` tool: pod resource usage from the metrics.k8s.io API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from kubernetes.client import CustomObjectsApi
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
