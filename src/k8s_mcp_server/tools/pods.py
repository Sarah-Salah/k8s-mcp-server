"""``list_pods`` tool: list pods, optionally filtered by namespace, labels, or fields."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import NamespaceNotAllowedError, resolve_read_namespaces
from k8s_mcp_server.tools._registry import ToolResult, register_tool
from k8s_mcp_server.utils.formatting import age_human, age_seconds_since

logger = logging.getLogger(__name__)


class ListPodsInput(BaseModel):
    """Inputs for ``list_pods``."""

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    label_selector: str | None = None
    field_selector: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


@register_tool(
    name="list_pods",
    description=(
        "List pods, optionally filtered by namespace, label selector, or field "
        "selector. Pass namespace='all' for every namespace the server is allowed "
        "to see; omit it to use the kubeconfig context's default namespace."
    ),
    input_model=ListPodsInput,
)
async def list_pods(
    inp: ListPodsInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """List pods, optionally filtered by namespace, labels, or fields.

    Honours ``--namespaces`` allowlist (per docs/TOOLS_SPEC.md). Output is
    sorted by ``(namespace, name)`` for stable consumption by LLMs.
    """
    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    api = CoreV1Api(ctx.api_client)
    try:
        raw = await _fetch_pods(api, targets, inp)
    except ApiException as exc:
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("list_pods failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    pods = [_format_pod(p) for p in raw]
    pods.sort(key=lambda p: (p["namespace"] or "", p["name"] or ""))
    truncated = len(pods) > inp.limit
    return ToolResult(
        success=True,
        data={"pods": pods[: inp.limit], "truncated": truncated},
    )


async def _fetch_pods(api: CoreV1Api, targets: list[str] | None, inp: ListPodsInput) -> list[Any]:
    if targets is None:
        res = await asyncio.to_thread(
            api.list_pod_for_all_namespaces,
            label_selector=inp.label_selector,
            field_selector=inp.field_selector,
            limit=inp.limit,
        )
        return list(res.items)

    collected: list[Any] = []
    for ns in targets:
        res = await asyncio.to_thread(
            api.list_namespaced_pod,
            namespace=ns,
            label_selector=inp.label_selector,
            field_selector=inp.field_selector,
            limit=inp.limit,
        )
        collected.extend(res.items)
    return collected


def _format_pod(pod: Any) -> dict[str, Any]:
    """Trim a V1Pod into the LLM-friendly shape from TOOLS_SPEC.md."""
    metadata = getattr(pod, "metadata", None)
    spec = getattr(pod, "spec", None)
    status = getattr(pod, "status", None)

    name = getattr(metadata, "name", None) if metadata else None
    namespace = getattr(metadata, "namespace", None) if metadata else None
    creation = getattr(metadata, "creation_timestamp", None) if metadata else None

    container_statuses = (getattr(status, "container_statuses", None) or []) if status else []
    ready_count = sum(1 for c in container_statuses if getattr(c, "ready", False))
    restart_total = sum(getattr(c, "restart_count", 0) or 0 for c in container_statuses)

    secs = age_seconds_since(creation)
    return {
        "name": name or "Unknown",
        "namespace": namespace or "Unknown",
        "phase": (getattr(status, "phase", None) if status else None) or "Unknown",
        "ready": f"{ready_count}/{len(container_statuses)}",
        "restarts": restart_total,
        "age_seconds": secs,
        "age_human": age_human(secs),
        "node": getattr(spec, "node_name", None) if spec else None,
        "pod_ip": getattr(status, "pod_ip", None) if status else None,
    }
