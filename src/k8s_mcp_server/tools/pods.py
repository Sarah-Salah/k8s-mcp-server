"""Pod tools: ``list_pods`` and ``get_pod``."""

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
from k8s_mcp_server.utils.k8s_conditions import format_condition
from k8s_mcp_server.utils.k8s_events import event_sort_key

logger = logging.getLogger(__name__)

_POD_EVENT_LIMIT = 10


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


class GetPodInput(BaseModel):
    """Inputs for ``get_pod``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    namespace: str | None = None


@register_tool(
    name="get_pod",
    description=(
        "Get a single pod's full state, including container statuses, "
        "conditions, and recent events. Defaults to the kubeconfig context's "
        "default namespace if not specified. namespace='all' is rejected — "
        "specify a single namespace."
    ),
    input_model=GetPodInput,
)
async def get_pod(
    inp: GetPodInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Get a single pod's full state, including container statuses,
    conditions, and recent events.
    """
    if inp.namespace == "all":
        return ToolResult(
            success=False,
            error="namespace='all' is not supported for get_pod; specify a single namespace",
        )

    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    # After the "all" guard, the resolver always returns a single-element list.
    assert targets is not None and len(targets) == 1
    namespace = targets[0]

    api = CoreV1Api(ctx.api_client)
    try:
        pod = await asyncio.to_thread(api.read_namespaced_pod, name=inp.name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ToolResult(
                success=False,
                error=f"pod '{inp.name}' not found in namespace '{namespace}'",
            )
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("get_pod failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    events = await _fetch_pod_events(api, namespace=namespace, pod_name=inp.name)
    return ToolResult(success=True, data=_format_pod_detail(pod, events))


async def _fetch_pod_events(
    api: CoreV1Api, *, namespace: str, pod_name: str
) -> list[dict[str, Any]]:
    """Fetch recent events for a pod by ``involvedObject.kind/name`` (not UID).

    UID-based filtering would drop kubelet-emitted events whose
    ``involvedObject.uid`` is null. See
    `[[project-event-filter-kind-name-not-uid]]` in agent memory for context.
    """
    field_selector = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
    try:
        result = await asyncio.to_thread(
            api.list_namespaced_event,
            namespace=namespace,
            field_selector=field_selector,
        )
    except ApiException:
        logger.warning("get_pod: failed to fetch events for %s/%s", namespace, pod_name)
        return []
    except Exception:
        logger.exception("get_pod: unexpected error fetching events for %s/%s", namespace, pod_name)
        return []

    events = sorted(result.items, key=event_sort_key, reverse=True)
    return [_format_event(e) for e in events[:_POD_EVENT_LIMIT]]


def _format_pod_detail(pod: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Compose the full ``get_pod`` response from a V1Pod and pre-formatted events."""
    metadata = getattr(pod, "metadata", None)
    spec = getattr(pod, "spec", None)
    status = getattr(pod, "status", None)

    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    container_statuses = (getattr(status, "container_statuses", None) or []) if status else []
    init_container_statuses = (
        (getattr(status, "init_container_statuses", None) or []) if status else []
    )
    conditions = (getattr(status, "conditions", None) or []) if status else []

    return {
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "namespace": (getattr(metadata, "namespace", None) if metadata else None) or "Unknown",
        "phase": (getattr(status, "phase", None) if status else None) or "Unknown",
        "node": getattr(spec, "node_name", None) if spec else None,
        "pod_ip": getattr(status, "pod_ip", None) if status else None,
        "age_seconds": secs,
        "age_human": age_human(secs),
        "containers": [_format_container_status(c) for c in container_statuses],
        "init_containers": [_format_container_status(c) for c in init_container_statuses],
        "conditions": [format_condition(c) for c in conditions],
        "events": events,
    }


def _format_container_status(c: Any) -> dict[str, Any]:
    state = getattr(c, "state", None)
    state_phase = "unknown"
    state_reason: str | None = None
    state_message: str | None = None
    if state is not None:
        if getattr(state, "running", None) is not None:
            state_phase = "running"
        elif getattr(state, "waiting", None) is not None:
            state_phase = "waiting"
            state_reason = getattr(state.waiting, "reason", None)
            state_message = getattr(state.waiting, "message", None)
        elif getattr(state, "terminated", None) is not None:
            state_phase = "terminated"
            state_reason = getattr(state.terminated, "reason", None)
            state_message = getattr(state.terminated, "message", None)

    return {
        "name": getattr(c, "name", None) or "Unknown",
        "image": getattr(c, "image", None),
        "ready": bool(getattr(c, "ready", False)),
        "restart_count": getattr(c, "restart_count", 0) or 0,
        "state": {
            "phase": state_phase,
            "reason": state_reason,
            "message": state_message,
        },
    }


def _format_event(event: Any) -> dict[str, Any]:
    last = getattr(event, "last_timestamp", None) or getattr(event, "event_time", None)
    first = getattr(event, "first_timestamp", None)
    return {
        "type": getattr(event, "type", None),
        "reason": getattr(event, "reason", None),
        "message": getattr(event, "message", None),
        "count": getattr(event, "count", None) or 1,
        "first_seen_age_seconds": age_seconds_since(first) if first is not None else None,
        "last_seen_age_seconds": age_seconds_since(last) if last is not None else None,
    }
