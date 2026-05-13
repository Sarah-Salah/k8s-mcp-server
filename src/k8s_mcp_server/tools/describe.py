"""``describe_resource`` tool: polymorphic structured 'describe' view.

Supports kinds: pod, deployment, service, node, configmap, secret, ingress.

The response shape is consistent across kinds:
``{kind, name, namespace, metadata, spec_summary, status, events}`` — an LLM
can rely on the schema. For ``kind="secret"`` only key names are returned;
never ``.data`` or ``.stringData`` values, per docs/SECURITY.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from kubernetes.client import AppsV1Api, CoreV1Api, NetworkingV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import NamespaceNotAllowedError, resolve_read_namespaces
from k8s_mcp_server.tools._registry import ToolResult, register_tool
from k8s_mcp_server.utils.formatting import age_human, age_seconds_since
from k8s_mcp_server.utils.k8s_events import event_sort_key

logger = logging.getLogger(__name__)

_DESCRIBE_EVENT_LIMIT = 5

# Annotations stripped when ``kind=secret``.
# ``kubectl.kubernetes.io/last-applied-configuration`` embeds the full applied
# JSON of the resource; for Secrets applied via ``kubectl apply -f`` that JSON
# contains the base64-encoded ``.data`` block, defeating the point of
# redacting ``.data`` from ``spec_summary``.
_SECRET_SENSITIVE_ANNOTATIONS = frozenset({"kubectl.kubernetes.io/last-applied-configuration"})


_Kind = Literal["pod", "deployment", "service", "node", "configmap", "secret", "ingress"]


class DescribeResourceInput(BaseModel):
    """Inputs for ``describe_resource``."""

    model_config = ConfigDict(extra="forbid")

    kind: _Kind
    name: str = Field(..., min_length=1)
    namespace: str | None = None


@dataclass(frozen=True, slots=True)
class _Describer:
    """Per-kind dispatch entry.

    ``fetch`` returns the V1<Kind> object; ``summarize`` returns
    ``(spec_summary, status)`` dicts.
    """

    kind_name: str  # API kind name (Pod, Deployment, ...) — used for event field selector
    namespaced: bool
    fetch_events: bool
    fetch: Callable[[KubeContext, str, str | None], Awaitable[Any]]
    summarize: Callable[[Any], tuple[dict[str, Any], dict[str, Any]]]


# ---------------------------------------------------------------------------
# Per-kind fetchers
# ---------------------------------------------------------------------------


async def _fetch_pod(ctx: KubeContext, name: str, namespace: str | None) -> Any:
    api = CoreV1Api(ctx.api_client)
    return await asyncio.to_thread(api.read_namespaced_pod, name=name, namespace=namespace)


async def _fetch_deployment(ctx: KubeContext, name: str, namespace: str | None) -> Any:
    api = AppsV1Api(ctx.api_client)
    return await asyncio.to_thread(api.read_namespaced_deployment, name=name, namespace=namespace)


async def _fetch_service(ctx: KubeContext, name: str, namespace: str | None) -> Any:
    api = CoreV1Api(ctx.api_client)
    return await asyncio.to_thread(api.read_namespaced_service, name=name, namespace=namespace)


async def _fetch_node(ctx: KubeContext, name: str, namespace: str | None) -> Any:
    del namespace  # Node is cluster-scoped
    api = CoreV1Api(ctx.api_client)
    return await asyncio.to_thread(api.read_node, name=name)


async def _fetch_configmap(ctx: KubeContext, name: str, namespace: str | None) -> Any:
    api = CoreV1Api(ctx.api_client)
    return await asyncio.to_thread(api.read_namespaced_config_map, name=name, namespace=namespace)


async def _fetch_secret(ctx: KubeContext, name: str, namespace: str | None) -> Any:
    api = CoreV1Api(ctx.api_client)
    return await asyncio.to_thread(api.read_namespaced_secret, name=name, namespace=namespace)


async def _fetch_ingress(ctx: KubeContext, name: str, namespace: str | None) -> Any:
    api = NetworkingV1Api(ctx.api_client)
    return await asyncio.to_thread(api.read_namespaced_ingress, name=name, namespace=namespace)


# ---------------------------------------------------------------------------
# Per-kind summarizers
# ---------------------------------------------------------------------------


def _summarize_pod(pod: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = getattr(pod, "spec", None)
    status = getattr(pod, "status", None)
    containers = (getattr(spec, "containers", None) or []) if spec else []
    return (
        {
            "containers": [
                {"name": getattr(c, "name", None), "image": getattr(c, "image", None)}
                for c in containers
            ],
            "node_name": getattr(spec, "node_name", None) if spec else None,
            "restart_policy": getattr(spec, "restart_policy", None) if spec else None,
        },
        {
            "phase": getattr(status, "phase", None) if status else None,
            "pod_ip": getattr(status, "pod_ip", None) if status else None,
        },
    )


def _summarize_deployment(d: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = getattr(d, "spec", None)
    status = getattr(d, "status", None)
    strategy = getattr(spec, "strategy", None) if spec else None
    selector = getattr(spec, "selector", None) if spec else None
    return (
        {
            "replicas": getattr(spec, "replicas", None) if spec else None,
            "strategy": getattr(strategy, "type", None) if strategy else None,
            "match_labels": (
                dict(getattr(selector, "match_labels", None) or {}) if selector else {}
            ),
        },
        {
            "replicas_ready": (getattr(status, "ready_replicas", 0) or 0) if status else 0,
            "replicas_available": (
                (getattr(status, "available_replicas", 0) or 0) if status else 0
            ),
        },
    )


def _summarize_service(s: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = getattr(s, "spec", None)
    status = getattr(s, "status", None)
    ports_raw = (getattr(spec, "ports", None) or []) if spec else []
    return (
        {
            "type": getattr(spec, "type", None) if spec else None,
            "cluster_ip": getattr(spec, "cluster_ip", None) if spec else None,
            "ports": [
                {
                    "port": getattr(p, "port", None),
                    "protocol": getattr(p, "protocol", None) or "TCP",
                }
                for p in ports_raw
            ],
        },
        {"load_balancer_ingress": _lb_ingress_summary(status)},
    )


def _summarize_node(n: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = getattr(n, "spec", None)
    status = getattr(n, "status", None)
    node_info = getattr(status, "node_info", None) if status else None
    capacity = (getattr(status, "capacity", None) or {}) if status else {}
    taints = (getattr(spec, "taints", None) or []) if spec else []
    conditions = (getattr(status, "conditions", None) or []) if status else []
    return (
        {
            "kubelet_version": (getattr(node_info, "kubelet_version", None) if node_info else None),
            "capacity": dict(capacity),
            "taints": [
                {
                    "key": getattr(t, "key", None),
                    "value": getattr(t, "value", None),
                    "effect": getattr(t, "effect", None),
                }
                for t in taints
            ],
        },
        {"ready_status": _ready_status_from_conditions(conditions)},
    )


def _summarize_configmap(cm: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    data = getattr(cm, "data", None) or {}
    binary_data = getattr(cm, "binary_data", None) or {}
    return (
        {
            "data_keys": sorted(data.keys()),
            "binary_data_keys": sorted(binary_data.keys()),
        },
        {},  # ConfigMap has no .status
    )


def _summarize_secret(s: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    # SECURITY-CRITICAL: never return Secret values, only key names.
    # See docs/SECURITY.md "Sensitive Data Handling".
    # Companion mitigation in _format_describe_response strips
    # `kubectl.kubernetes.io/last-applied-configuration` which can embed the
    # base64-encoded data block via `kubectl apply -f`.
    data = getattr(s, "data", None) or {}
    string_data = getattr(s, "string_data", None) or {}
    return (
        {
            "type": getattr(s, "type", None),
            # Union of .data and .stringData KEY NAMES — never values.
            "data_keys": sorted(set(data.keys()) | set(string_data.keys())),
        },
        {},  # Secret has no .status
    )


def _summarize_ingress(ing: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = getattr(ing, "spec", None)
    status = getattr(ing, "status", None)
    rules = (getattr(spec, "rules", None) or []) if spec else []
    return (
        {"rules": [_summarize_ingress_rule(r) for r in rules]},
        {"load_balancer_ingress": _lb_ingress_summary(status)},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# DUPLICATION: also defined in src/k8s_mcp_server/tools/nodes.py as _ready_status.
# Inlined here because cross-tool-module imports increase coupling, and the
# rule of three (third caller) is not yet met. If a third tool needs Ready
# derivation, extract to a shared utils module then.
def _ready_status_from_conditions(conditions: list[Any]) -> str:
    for c in conditions:
        if getattr(c, "type", None) == "Ready":
            status = getattr(c, "status", None)
            if status == "True":
                return "Ready"
            if status == "False":
                return "NotReady"
            return "Unknown"
    return "Unknown"


def _lb_ingress_summary(status: Any) -> list[dict[str, Any]]:
    lb = getattr(status, "load_balancer", None) if status else None
    ingress = (getattr(lb, "ingress", None) or []) if lb else []
    return [
        {"ip": getattr(i, "ip", None), "hostname": getattr(i, "hostname", None)} for i in ingress
    ]


def _summarize_ingress_rule(rule: Any) -> dict[str, Any]:
    http = getattr(rule, "http", None)
    paths = (getattr(http, "paths", None) or []) if http else []
    return {
        "host": getattr(rule, "host", None),
        "paths": [_summarize_ingress_path(p) for p in paths],
    }


def _summarize_ingress_path(p: Any) -> dict[str, Any]:
    return {
        "path": getattr(p, "path", None),
        "path_type": getattr(p, "path_type", None),
        "backend": _summarize_ingress_backend(p),
    }


def _summarize_ingress_backend(p: Any) -> dict[str, Any]:
    backend = getattr(p, "backend", None)
    service = getattr(backend, "service", None) if backend else None
    return {
        "service_name": getattr(service, "name", None) if service else None,
        "service_port": _ingress_service_port(service),
    }


def _ingress_service_port(service: Any) -> int | str | None:
    if service is None:
        return None
    port = getattr(service, "port", None)
    if port is None:
        return None
    num = getattr(port, "number", None)
    if num is not None:
        return int(num)
    name = getattr(port, "name", None)
    return str(name) if name is not None else None


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


_DESCRIBERS: dict[str, _Describer] = {
    "pod": _Describer("Pod", True, True, _fetch_pod, _summarize_pod),
    "deployment": _Describer("Deployment", True, True, _fetch_deployment, _summarize_deployment),
    "service": _Describer("Service", True, True, _fetch_service, _summarize_service),
    "node": _Describer("Node", False, False, _fetch_node, _summarize_node),
    "configmap": _Describer("ConfigMap", True, False, _fetch_configmap, _summarize_configmap),
    "secret": _Describer("Secret", True, False, _fetch_secret, _summarize_secret),
    "ingress": _Describer("Ingress", True, True, _fetch_ingress, _summarize_ingress),
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@register_tool(
    name="describe_resource",
    description=(
        "Structured 'describe' view of any standard K8s resource: pod, "
        "deployment, service, node, configmap, secret, or ingress. Returns "
        "metadata + kind-specific spec_summary + status + recent events (for "
        "event-generating namespaced kinds: pod, deployment, service, ingress). "
        "For kind='secret', only key names are returned — never .data or "
        ".stringData values. namespace='all' is rejected; cluster-scoped "
        "kinds (node) reject the namespace input entirely."
    ),
    input_model=DescribeResourceInput,
)
async def describe_resource(
    inp: DescribeResourceInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Polymorphic structured describe for the seven supported kinds."""
    describer = _DESCRIBERS[inp.kind]

    if not describer.namespaced:
        if inp.namespace is not None:
            return ToolResult(
                success=False,
                error=f"kind='{inp.kind}' does not accept a namespace parameter",
            )
        namespace: str | None = None
    else:
        if inp.namespace == "all":
            return ToolResult(
                success=False,
                error=(
                    "namespace='all' is not supported for describe_resource; "
                    "specify a single namespace"
                ),
            )
        try:
            targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
        except NamespaceNotAllowedError as exc:
            return ToolResult(success=False, error=str(exc))
        # After the "all" guard above, the resolver always returns a single-element list.
        assert targets is not None and len(targets) == 1
        namespace = targets[0]

    try:
        obj = await describer.fetch(ctx, inp.name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ToolResult(
                success=False,
                error=_not_found_message(describer.kind_name, inp.name, namespace),
            )
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("describe_resource failed for kind=%s", inp.kind)
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    events: list[dict[str, Any]] = []
    if describer.fetch_events and namespace is not None:
        events = await _fetch_describe_events(
            ctx, kind=describer.kind_name, name=inp.name, namespace=namespace
        )

    return ToolResult(
        success=True,
        data=_format_describe_response(obj, describer=describer, events=events),
    )


def _not_found_message(kind_name: str, name: str, namespace: str | None) -> str:
    """Build a friendly 404 message, e.g. ``"pod 'X' not found in namespace 'Y'"``."""
    kind_lc = kind_name.lower()
    if namespace is None:
        return f"{kind_lc} '{name}' not found"
    return f"{kind_lc} '{name}' not found in namespace '{namespace}'"


async def _fetch_describe_events(
    ctx: KubeContext, *, kind: str, name: str, namespace: str
) -> list[dict[str, Any]]:
    """Fetch the most recent events for the resource.

    Uses ``involvedObject.kind=<kind>,involvedObject.name=<name>`` — kind+name,
    NOT UID (see ``project-event-filter-kind-name-not-uid`` in agent memory).
    Failure → empty list with a warning (partial-success pattern matches
    ``get_pod``'s event fetch and ``get_deployment``'s RS fetch).
    """
    field_selector = f"involvedObject.kind={kind},involvedObject.name={name}"
    api = CoreV1Api(ctx.api_client)
    try:
        result = await asyncio.to_thread(
            api.list_namespaced_event,
            namespace=namespace,
            field_selector=field_selector,
        )
    except ApiException:
        logger.warning(
            "describe_resource: failed to fetch events for %s/%s/%s",
            namespace,
            kind,
            name,
        )
        return []
    except Exception:
        logger.exception(
            "describe_resource: unexpected event error for %s/%s/%s",
            namespace,
            kind,
            name,
        )
        return []
    events = sorted(result.items, key=event_sort_key, reverse=True)
    return [_format_describe_event(e) for e in events[:_DESCRIBE_EVENT_LIMIT]]


# DUPLICATION: this is a slimmer variant of _format_event in
# src/k8s_mcp_server/tools/pods.py and src/k8s_mcp_server/tools/events.py.
# This version omits ``involved_object`` (redundant here — we filtered events
# by kind+name upfront). The three near-duplicates will be consolidated in
# a follow-up refactor commit; the eventual shape (tuple-returning helper or
# base+extension) is intentionally left open. See CHANGELOG "Known
# duplication".
def _format_describe_event(e: Any) -> dict[str, Any]:
    last = getattr(e, "last_timestamp", None) or getattr(e, "event_time", None)
    first = getattr(e, "first_timestamp", None)
    return {
        "type": getattr(e, "type", None),
        "reason": getattr(e, "reason", None),
        "message": getattr(e, "message", None),
        "count": getattr(e, "count", None) or 1,
        "first_seen_age_seconds": age_seconds_since(first) if first is not None else None,
        "last_seen_age_seconds": age_seconds_since(last) if last is not None else None,
    }


def _format_describe_response(
    obj: Any, *, describer: _Describer, events: list[dict[str, Any]]
) -> dict[str, Any]:
    metadata = getattr(obj, "metadata", None)
    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    labels = dict(getattr(metadata, "labels", None) or {}) if metadata else {}
    annotations_raw = dict(getattr(metadata, "annotations", None) or {}) if metadata else {}
    annotations = (
        _strip_secret_sensitive_annotations(annotations_raw)
        if describer.kind_name == "Secret"
        else annotations_raw
    )

    spec_summary, status = describer.summarize(obj)

    return {
        "kind": describer.kind_name,
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "namespace": getattr(metadata, "namespace", None) if metadata else None,
        "metadata": {
            "labels": labels,
            "annotations": annotations,
            "creation_timestamp": creation.isoformat() if creation else None,
            "age_seconds": secs,
            "age_human": age_human(secs),
            "uid": getattr(metadata, "uid", None) if metadata else None,
        },
        "spec_summary": spec_summary,
        "status": status,
        "events": events,
    }


def _strip_secret_sensitive_annotations(annotations: dict[str, Any]) -> dict[str, Any]:
    """Remove annotations known to leak Secret values.

    SECURITY-CRITICAL companion to :func:`_summarize_secret`. The
    ``kubectl.kubernetes.io/last-applied-configuration`` annotation embeds
    the full applied JSON of the resource — for Secrets applied via
    ``kubectl apply -f``, that JSON includes the base64-encoded data block.
    Returning it would defeat the redaction in ``_summarize_secret``.
    """
    return {k: v for k, v in annotations.items() if k not in _SECRET_SENSITIVE_ANNOTATIONS}
