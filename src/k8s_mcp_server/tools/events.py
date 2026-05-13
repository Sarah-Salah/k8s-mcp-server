"""``list_events`` tool: cluster events filtered by kind/name/type/since."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import NamespaceNotAllowedError, resolve_read_namespaces
from k8s_mcp_server.tools._registry import ToolResult, register_tool
from k8s_mcp_server.utils.formatting import age_seconds_since

logger = logging.getLogger(__name__)


class ListEventsInput(BaseModel):
    """Inputs for ``list_events``."""

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = None
    involved_object_kind: str | None = None
    involved_object_name: str | None = None
    type: Literal["Normal", "Warning"] | None = None
    since_seconds: int | None = Field(default=None, ge=1)
    limit: int = Field(default=50, ge=1, le=1000)


@register_tool(
    name="list_events",
    description=(
        "Get cluster events, filtered by namespace / involved object kind+name / "
        "type ('Normal' or 'Warning') / since_seconds, sorted most-recent first. "
        "namespace='all' resolves to every allowlisted namespace (or the whole "
        "cluster if no allowlist is configured)."
    ),
    input_model=ListEventsInput,
)
async def list_events(
    inp: ListEventsInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """List cluster events, sorted most-recent first.

    Honours ``--namespaces`` allowlist via ``resolve_read_namespaces``: if
    set, ``"all"`` resolves to the allowlist (per-namespace API calls);
    without an allowlist, ``"all"`` calls ``list_event_for_all_namespaces``
    once.
    """
    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    field_selector = _build_field_selector(
        kind=inp.involved_object_kind,
        name=inp.involved_object_name,
        type_=inp.type,
    )

    api = CoreV1Api(ctx.api_client)
    try:
        raw = await _fetch_events(api, targets, field_selector=field_selector, limit=inp.limit)
    except ApiException as exc:
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("list_events failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    if inp.since_seconds is not None:
        raw = [e for e in raw if _within_since(e, threshold_seconds=inp.since_seconds)]

    raw.sort(key=_event_sort_key, reverse=True)
    truncated = len(raw) > inp.limit
    events = [_format_event(e) for e in raw[: inp.limit]]
    return ToolResult(success=True, data={"events": events, "truncated": truncated})


async def _fetch_events(
    api: CoreV1Api,
    targets: list[str] | None,
    *,
    field_selector: str | None,
    limit: int,
) -> list[Any]:
    if targets is None:
        res = await asyncio.to_thread(
            api.list_event_for_all_namespaces,
            field_selector=field_selector,
            limit=limit,
        )
        return list(res.items)

    collected: list[Any] = []
    for ns in targets:
        res = await asyncio.to_thread(
            api.list_namespaced_event,
            namespace=ns,
            field_selector=field_selector,
            limit=limit,
        )
        collected.extend(res.items)
    return collected


def _build_field_selector(*, kind: str | None, name: str | None, type_: str | None) -> str | None:
    parts: list[str] = []
    if kind:
        parts.append(f"involvedObject.kind={kind}")
    if name:
        parts.append(f"involvedObject.name={name}")
    if type_:
        parts.append(f"type={type_}")
    return ",".join(parts) if parts else None


def _within_since(event: Any, *, threshold_seconds: int) -> bool:
    """Keep events whose most-recent timestamp is within ``threshold_seconds`` of now.

    Events with no usable timestamps fall back to epoch UTC (see
    ``_event_sort_key``) and are therefore filtered out.
    """
    last = _event_sort_key(event)
    age = (datetime.now(UTC) - last).total_seconds()
    return bool(age <= threshold_seconds)


# DUPLICATION: this function is also defined in src/k8s_mcp_server/tools/pods.py.
# Both copies will be replaced by a shared helper at
# src/k8s_mcp_server/utils/k8s_events.py in the next commit (refactor:
# extract _event_sort_key). Maintaining two copies briefly so this commit
# stays single-purpose.
def _event_sort_key(event: Any) -> Any:
    """Most-recent timestamp for ordering events; falls back to epoch UTC."""
    for attr in ("last_timestamp", "event_time"):
        value = getattr(event, attr, None)
        if value is not None:
            return value
    metadata = getattr(event, "metadata", None)
    if metadata is not None:
        ct = getattr(metadata, "creation_timestamp", None)
        if ct is not None:
            return ct
    return datetime(1970, 1, 1, tzinfo=UTC)


def _format_event(event: Any) -> dict[str, Any]:
    last = getattr(event, "last_timestamp", None) or getattr(event, "event_time", None)
    first = getattr(event, "first_timestamp", None)
    inv = getattr(event, "involved_object", None)
    return {
        "type": getattr(event, "type", None),
        "reason": getattr(event, "reason", None),
        "message": getattr(event, "message", None),
        "count": getattr(event, "count", None) or 1,
        "first_seen_age_seconds": age_seconds_since(first) if first is not None else None,
        "last_seen_age_seconds": age_seconds_since(last) if last is not None else None,
        "involved_object": {
            "kind": getattr(inv, "kind", None) if inv else None,
            "name": getattr(inv, "name", None) if inv else None,
            "namespace": getattr(inv, "namespace", None) if inv else None,
        },
    }
