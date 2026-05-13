"""``list_namespaces`` tool: list cluster namespaces (respects ``--namespaces`` allowlist)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools._registry import ToolResult, register_tool
from k8s_mcp_server.utils.formatting import age_human, age_seconds_since

logger = logging.getLogger(__name__)


class ListNamespacesInput(BaseModel):
    """No inputs. Defined for symmetry with other tools and to reject hallucinated args."""

    model_config = ConfigDict(extra="forbid")


@register_tool(
    name="list_namespaces",
    description="List all namespaces in the cluster, with status and age.",
    input_model=ListNamespacesInput,
)
async def list_namespaces(
    _input: ListNamespacesInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """List all namespaces in the cluster, with status and age.

    If ``--namespaces`` was set at server start, returns only the allowlisted
    namespaces (per docs/TOOLS_SPEC.md). Output is sorted by name for stable
    consumption by LLMs.
    """
    api = CoreV1Api(ctx.api_client)
    try:
        result = await asyncio.to_thread(api.list_namespace)
    except ApiException as exc:
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("list_namespace failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    allow = set(settings.namespaces) if settings.namespaces else None
    items: list[dict[str, Any]] = []
    for ns in result.items:
        name = ns.metadata.name
        if allow is not None and name not in allow:
            continue
        secs = age_seconds_since(ns.metadata.creation_timestamp)
        items.append(
            {
                "name": name,
                "status": (ns.status.phase if ns.status and ns.status.phase else "Unknown"),
                "age_seconds": secs,
                "age_human": age_human(secs),
            }
        )

    items.sort(key=lambda n: n["name"])
    return ToolResult(success=True, data={"namespaces": items})
