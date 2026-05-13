"""``list_deployments`` tool: list deployments, optionally filtered by namespace/labels."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes.client import AppsV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import NamespaceNotAllowedError, resolve_read_namespaces
from k8s_mcp_server.tools._registry import ToolResult, register_tool
from k8s_mcp_server.utils.formatting import age_human, age_seconds_since

logger = logging.getLogger(__name__)


class ListDeploymentsInput(BaseModel):
    """Inputs for ``list_deployments``."""

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    label_selector: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


@register_tool(
    name="list_deployments",
    description=(
        "List deployments, optionally filtered by namespace or label selector. "
        "Pass namespace='all' for every namespace the server is allowed to see; "
        "omit it to use the kubeconfig context's default namespace. Returns the "
        "primary container's image only — use get_deployment for full container "
        "lists and rollout state."
    ),
    input_model=ListDeploymentsInput,
)
async def list_deployments(
    inp: ListDeploymentsInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """List deployments, optionally filtered by namespace or label selector.

    Honours ``--namespaces`` allowlist (per docs/TOOLS_SPEC.md). Output is
    sorted by ``(namespace, name)`` for stable consumption by LLMs.
    """
    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    api = AppsV1Api(ctx.api_client)
    try:
        raw = await _fetch_deployments(api, targets, inp)
    except ApiException as exc:
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("list_deployments failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    deployments = [_format_deployment(d) for d in raw]
    deployments.sort(key=lambda d: (d["namespace"] or "", d["name"] or ""))
    truncated = len(deployments) > inp.limit
    return ToolResult(
        success=True,
        data={"deployments": deployments[: inp.limit], "truncated": truncated},
    )


async def _fetch_deployments(
    api: AppsV1Api, targets: list[str] | None, inp: ListDeploymentsInput
) -> list[Any]:
    if targets is None:
        res = await asyncio.to_thread(
            api.list_deployment_for_all_namespaces,
            label_selector=inp.label_selector,
            limit=inp.limit,
        )
        return list(res.items)

    collected: list[Any] = []
    for ns in targets:
        res = await asyncio.to_thread(
            api.list_namespaced_deployment,
            namespace=ns,
            label_selector=inp.label_selector,
            limit=inp.limit,
        )
        collected.extend(res.items)
    return collected


def _format_deployment(d: Any) -> dict[str, Any]:
    """Trim a V1Deployment into the LLM-friendly shape from TOOLS_SPEC.md."""
    metadata = getattr(d, "metadata", None)
    spec = getattr(d, "spec", None)
    status = getattr(d, "status", None)

    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    template = getattr(spec, "template", None) if spec else None
    pod_spec = getattr(template, "spec", None) if template else None
    containers = (getattr(pod_spec, "containers", None) or []) if pod_spec else []
    image = getattr(containers[0], "image", None) if containers else None

    return {
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "namespace": (getattr(metadata, "namespace", None) if metadata else None) or "Unknown",
        "replicas_desired": getattr(spec, "replicas", None) if spec else None,
        "replicas_ready": (getattr(status, "ready_replicas", 0) or 0) if status else 0,
        "age_seconds": secs,
        "age_human": age_human(secs),
        "image": image,
    }
