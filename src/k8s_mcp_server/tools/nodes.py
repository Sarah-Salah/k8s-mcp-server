"""Node tools: ``list_nodes`` and ``get_node``."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools._registry import ToolResult, register_tool
from k8s_mcp_server.utils.formatting import age_human, age_seconds_since
from k8s_mcp_server.utils.k8s_conditions import format_condition

logger = logging.getLogger(__name__)

_NODE_ROLE_LABEL_PREFIX = "node-role.kubernetes.io/"
_NODE_POD_COUNT_LIMIT = 1000  # kubelet default max pods/node is 110; 1000 is safety margin


class ListNodesInput(BaseModel):
    """Inputs for ``list_nodes``.

    No ``namespace`` field — nodes are cluster-scoped resources.
    """

    model_config = ConfigDict(extra="forbid")

    label_selector: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


@register_tool(
    name="list_nodes",
    description=(
        "List cluster nodes with health and capacity. Nodes are cluster-scoped "
        "so there is no namespace input. Output includes derived status "
        "(Ready/NotReady/Unknown), roles from node-role.kubernetes.io/* "
        "labels (falling back to 'worker'), kubelet version, and raw "
        "capacity/allocatable Quantity strings."
    ),
    input_model=ListNodesInput,
)
async def list_nodes(
    inp: ListNodesInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """List cluster nodes with health and capacity.

    Nodes are cluster-scoped — the ``--namespaces`` allowlist does not apply.
    Output is sorted by name for stable consumption by LLMs.
    """
    del settings  # nodes are cluster-scoped; no allowlist applies

    api = CoreV1Api(ctx.api_client)
    try:
        res = await asyncio.to_thread(
            api.list_node,
            label_selector=inp.label_selector,
            limit=inp.limit,
        )
    except ApiException as exc:
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("list_nodes failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    nodes = [_format_node(n) for n in res.items]
    nodes.sort(key=lambda n: n["name"] or "")
    truncated = len(nodes) > inp.limit
    return ToolResult(
        success=True,
        data={"nodes": nodes[: inp.limit], "truncated": truncated},
    )


def _format_node(n: Any) -> dict[str, Any]:
    """Trim a V1Node into the LLM-friendly shape from TOOLS_SPEC.md."""
    metadata = getattr(n, "metadata", None)
    status = getattr(n, "status", None)

    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    labels = (getattr(metadata, "labels", None) or {}) if metadata else {}
    conditions = (getattr(status, "conditions", None) or []) if status else []
    node_info = getattr(status, "node_info", None) if status else None
    capacity = (getattr(status, "capacity", None) or {}) if status else {}
    allocatable = (getattr(status, "allocatable", None) or {}) if status else {}

    return {
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "status": _ready_status(conditions),
        "roles": _roles_from_labels(labels),
        "age_seconds": secs,
        "age_human": age_human(secs),
        "kubelet_version": (getattr(node_info, "kubelet_version", None) if node_info else None),
        "capacity": dict(capacity),
        "allocatable": dict(allocatable),
    }


def _roles_from_labels(labels: dict[str, Any]) -> list[str]:
    """Extract roles from ``node-role.kubernetes.io/<role>`` labels.

    Falls back to ``["worker"]`` when no role labels are present (matches
    ``kubectl get nodes`` display behaviour for unlabelled worker pools).
    Bare ``node-role.kubernetes.io/`` labels with empty suffixes are skipped.
    """
    roles = sorted(
        suffix
        for label in labels
        if label.startswith(_NODE_ROLE_LABEL_PREFIX)
        and (suffix := label[len(_NODE_ROLE_LABEL_PREFIX) :])
    )
    return roles or ["worker"]


def _ready_status(conditions: list[Any]) -> str:
    """Derive Ready/NotReady/Unknown from V1NodeCondition list."""
    for c in conditions:
        if getattr(c, "type", None) == "Ready":
            status = getattr(c, "status", None)
            if status == "True":
                return "Ready"
            if status == "False":
                return "NotReady"
            return "Unknown"
    return "Unknown"


class GetNodeInput(BaseModel):
    """Inputs for ``get_node``.

    No ``namespace`` field — nodes are cluster-scoped resources.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)


@register_tool(
    name="get_node",
    description=(
        "Full node detail including conditions (with last_transition_age_seconds), "
        "taints, capacity/allocatable Quantity strings, and a pods-on-node count. "
        "Nodes are cluster-scoped — no namespace input."
    ),
    input_model=GetNodeInput,
)
async def get_node(
    inp: GetNodeInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Full node details including conditions and recent events."""
    del settings  # nodes are cluster-scoped; no allowlist applies

    api = CoreV1Api(ctx.api_client)
    try:
        node = await asyncio.to_thread(api.read_node, name=inp.name)
    except ApiException as exc:
        if exc.status == 404:
            return ToolResult(success=False, error=f"node '{inp.name}' not found")
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("get_node failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    pods_on_node = await _fetch_pod_count_on_node(api, node_name=inp.name)
    return ToolResult(success=True, data=_format_node_detail(node, pods_on_node))


async def _fetch_pod_count_on_node(api: CoreV1Api, *, node_name: str) -> int | None:
    """Count pods scheduled to ``node_name`` via the ``spec.nodeName`` field selector.

    Returns ``None`` (with a logged warning) on RBAC/API failure so the rest
    of the node detail still surfaces — same partial-success pattern as
    ``get_pod``'s event fetch and ``get_deployment``'s ReplicaSet fetch.
    """
    try:
        res = await asyncio.to_thread(
            api.list_pod_for_all_namespaces,
            field_selector=f"spec.nodeName={node_name}",
            limit=_NODE_POD_COUNT_LIMIT,
        )
    except ApiException:
        logger.warning("get_node: failed to fetch pod count for %s", node_name)
        return None
    except Exception:
        logger.exception("get_node: unexpected error fetching pod count for %s", node_name)
        return None
    return len(res.items)


def _format_node_detail(n: Any, pods_on_node: int | None) -> dict[str, Any]:
    """Compose the full ``get_node`` response from a V1Node and pre-counted pods."""
    metadata = getattr(n, "metadata", None)
    spec = getattr(n, "spec", None)
    status = getattr(n, "status", None)

    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    labels = (getattr(metadata, "labels", None) or {}) if metadata else {}
    conditions = (getattr(status, "conditions", None) or []) if status else []
    node_info = getattr(status, "node_info", None) if status else None
    capacity = (getattr(status, "capacity", None) or {}) if status else {}
    allocatable = (getattr(status, "allocatable", None) or {}) if status else {}
    taints = (getattr(spec, "taints", None) or []) if spec else []

    return {
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "status": _ready_status(conditions),
        "roles": _roles_from_labels(labels),
        "age_seconds": secs,
        "age_human": age_human(secs),
        "kubelet_version": (getattr(node_info, "kubelet_version", None) if node_info else None),
        "capacity": dict(capacity),
        "allocatable": dict(allocatable),
        "conditions": [format_condition(c) for c in conditions],
        "taints": [_format_taint(t) for t in taints],
        "pods_on_node": pods_on_node,
    }


def _format_taint(t: Any) -> dict[str, Any]:
    return {
        "key": getattr(t, "key", None),
        "value": getattr(t, "value", None),
        "effect": getattr(t, "effect", None),
    }
