"""``get_pod_logs`` tool: fetch pod logs with tail / since / previous filters."""

from __future__ import annotations

import asyncio
import logging

from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import NamespaceNotAllowedError, resolve_read_namespaces
from k8s_mcp_server.tools._registry import ToolResult, register_tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 256 * 1024  # 256 KiB
MIN_MAX_BYTES = 1024  # 1 KiB
MAX_MAX_BYTES = 1024 * 1024  # 1 MiB


class GetPodLogsInput(BaseModel):
    """Inputs for ``get_pod_logs``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    namespace: str | None = None
    container: str | None = None
    tail_lines: int = Field(default=200, ge=1, le=10000)
    since_seconds: int | None = Field(default=None, ge=1)
    previous: bool = False
    max_bytes: int = Field(default=DEFAULT_MAX_BYTES, ge=MIN_MAX_BYTES, le=MAX_MAX_BYTES)


@register_tool(
    name="get_pod_logs",
    description=(
        "Get logs from a pod. Use 'container' if the pod has multiple containers. "
        "Use previous=True to read logs from the prior crashed instance. "
        "tail_lines and since_seconds are passed to the K8s API directly. "
        "Output is capped at max_bytes (default 256 KiB) and trimmed from the "
        "start (most recent kept), with a partial first line dropped."
    ),
    input_model=GetPodLogsInput,
)
async def get_pod_logs(
    inp: GetPodLogsInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Get logs from a pod, optionally for a specific container, with tail and
    time-since filters.

    Known limitation (v1): ephemeral containers (added by ``kubectl debug``)
    are not auto-resolved by the single-container code path. They are still
    fetchable by passing ``container=<ephemeral-container-name>`` explicitly.
    v2 will surface ephemeral container names in the multi-container error
    message and consider them in auto-resolution.
    """
    if inp.namespace == "all":
        return ToolResult(
            success=False,
            error=("namespace='all' is not supported for get_pod_logs; specify a single namespace"),
        )

    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    assert targets is not None and len(targets) == 1
    namespace = targets[0]

    api = CoreV1Api(ctx.api_client)

    container = inp.container
    if container is None:
        try:
            container = await _resolve_container(api, name=inp.name, namespace=namespace)
        except _LogsToolError as exc:
            return ToolResult(success=False, error=str(exc))

    try:
        raw = await asyncio.to_thread(
            api.read_namespaced_pod_log,
            name=inp.name,
            namespace=namespace,
            container=container,
            tail_lines=inp.tail_lines,
            since_seconds=inp.since_seconds,
            previous=inp.previous,
        )
    except ApiException as exc:
        return ToolResult(
            success=False,
            error=_format_log_api_error(
                exc,
                name=inp.name,
                namespace=namespace,
                container=container,
                previous=inp.previous,
            ),
        )
    except Exception as exc:
        logger.exception("get_pod_logs failed (pod=%s/%s)", namespace, inp.name)
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    logs_text, truncated = _cap_bytes(raw or "", max_bytes=inp.max_bytes)
    # Metadata-only logging — never log raw log content (may contain PII,
    # credentials in stack traces, internal URLs, DB connection strings).
    logger.info(
        "get_pod_logs: pod=%s/%s container=%s bytes=%d truncated=%s previous=%s",
        namespace,
        inp.name,
        container,
        len(logs_text.encode("utf-8")),
        truncated,
        inp.previous,
    )
    return ToolResult(
        success=True,
        data={"logs": logs_text, "truncated": truncated, "container": container},
    )


class _LogsToolError(Exception):
    """Friendly tool-level error from the pre-flight container resolver."""


async def _resolve_container(api: CoreV1Api, *, name: str, namespace: str) -> str:
    """Pre-flight: read the pod and decide which container's logs to fetch.

    Auto-picks the sole regular container if there is only one. Errors out
    with the container list if there are several. Ephemeral containers are
    intentionally NOT considered here (see ``get_pod_logs`` docstring).
    """
    try:
        pod = await asyncio.to_thread(api.read_namespaced_pod, name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise _LogsToolError(f"pod '{name}' not found in namespace '{namespace}'") from exc
        raise _LogsToolError(f"kubernetes API error: {exc.reason or exc.status}") from exc

    spec = getattr(pod, "spec", None)
    containers = (getattr(spec, "containers", None) or []) if spec is not None else []
    if not containers:
        raise _LogsToolError(f"pod '{name}' has no containers defined")
    if len(containers) > 1:
        names = [getattr(c, "name", "?") for c in containers]
        raise _LogsToolError(
            f"pod '{name}' has multiple containers ({names}); "
            f"specify one with the 'container' parameter"
        )
    return getattr(containers[0], "name", "") or ""


def _cap_bytes(text: str, *, max_bytes: int) -> tuple[str, bool]:
    """Cap log text to ``max_bytes`` bytes, dropping from the start."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    decoded = data[-max_bytes:].decode("utf-8", errors="replace")
    if "\n" in decoded:
        decoded = decoded[decoded.index("\n") + 1 :]
    return decoded, True


def _format_log_api_error(
    exc: ApiException,
    *,
    name: str,
    namespace: str,
    container: str | None,
    previous: bool,
) -> str:
    if exc.status == 404:
        return f"pod '{name}' not found in namespace '{namespace}'"
    if exc.status == 400 and previous:
        ctx_str = f" container '{container}'" if container else ""
        return (
            f"no previous logs for pod '{name}'{ctx_str}: the container has "
            f"not been restarted, or no previous instance exists"
        )
    return f"kubernetes API error: {exc.reason or exc.status}"
