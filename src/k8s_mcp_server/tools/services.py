"""``list_services`` tool: list services, optionally filtered by namespace/labels."""

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


class ListServicesInput(BaseModel):
    """Inputs for ``list_services``."""

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    label_selector: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


@register_tool(
    name="list_services",
    description=(
        "List services, optionally filtered by namespace or label selector. "
        "Pass namespace='all' for every namespace the server is allowed to "
        "see; omit it to use the kubeconfig context's default namespace. "
        "external_ip is populated only for LoadBalancer services with a "
        "provisioned ingress (None otherwise, including ExternalName)."
    ),
    input_model=ListServicesInput,
)
async def list_services(
    inp: ListServicesInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """List services, optionally filtered by namespace or label selector.

    Honours ``--namespaces`` allowlist (per docs/TOOLS_SPEC.md). Output is
    sorted by ``(namespace, name)`` for stable consumption by LLMs.
    """
    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    api = CoreV1Api(ctx.api_client)
    try:
        raw = await _fetch_services(api, targets, inp)
    except ApiException as exc:
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("list_services failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    services = [_format_service(s) for s in raw]
    services.sort(key=lambda s: (s["namespace"] or "", s["name"] or ""))
    truncated = len(services) > inp.limit
    return ToolResult(
        success=True,
        data={"services": services[: inp.limit], "truncated": truncated},
    )


async def _fetch_services(
    api: CoreV1Api, targets: list[str] | None, inp: ListServicesInput
) -> list[Any]:
    if targets is None:
        res = await asyncio.to_thread(
            api.list_service_for_all_namespaces,
            label_selector=inp.label_selector,
            limit=inp.limit,
        )
        return list(res.items)

    collected: list[Any] = []
    for ns in targets:
        res = await asyncio.to_thread(
            api.list_namespaced_service,
            namespace=ns,
            label_selector=inp.label_selector,
            limit=inp.limit,
        )
        collected.extend(res.items)
    return collected


def _format_service(s: Any) -> dict[str, Any]:
    """Trim a V1Service into the LLM-friendly shape from TOOLS_SPEC.md."""
    metadata = getattr(s, "metadata", None)
    spec = getattr(s, "spec", None)
    status = getattr(s, "status", None)

    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    ports_raw = (getattr(spec, "ports", None) or []) if spec else []

    return {
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "namespace": (getattr(metadata, "namespace", None) if metadata else None) or "Unknown",
        "type": getattr(spec, "type", None) if spec else None,
        "cluster_ip": getattr(spec, "cluster_ip", None) if spec else None,
        "external_ip": _external_ip_of(status),
        "ports": [_format_port(p) for p in ports_raw],
        "age_seconds": secs,
        "age_human": age_human(secs),
    }


def _external_ip_of(status: Any) -> str | None:
    """Return the external IP or hostname from the first LoadBalancer ingress entry."""
    lb = getattr(status, "load_balancer", None) if status else None
    ingress = (getattr(lb, "ingress", None) or []) if lb else []
    if not ingress:
        return None
    first = ingress[0]
    ip = getattr(first, "ip", None)
    if ip:
        return str(ip)
    hostname = getattr(first, "hostname", None)
    return str(hostname) if hostname else None


def _format_port(p: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": getattr(p, "name", None),
        "port": getattr(p, "port", None),
        "target_port": getattr(p, "target_port", None),
        "protocol": getattr(p, "protocol", None) or "TCP",
    }
    node_port = getattr(p, "node_port", None)
    if node_port is not None:
        out["node_port"] = node_port
    return out
