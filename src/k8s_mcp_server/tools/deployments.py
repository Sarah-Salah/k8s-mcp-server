"""Deployment tools: list/get/scale/restart_deployment."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from kubernetes.client import AppsV1Api
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import (
    NamespaceNotAllowedError,
    assert_writes_enabled,
    resolve_read_namespaces,
)
from k8s_mcp_server.tools._registry import ToolResult, register_tool
from k8s_mcp_server.utils.audit import log_write_operation
from k8s_mcp_server.utils.formatting import age_human, age_seconds_since
from k8s_mcp_server.utils.k8s_conditions import format_condition

logger = logging.getLogger(__name__)

_DEPLOYMENT_REVISION_LIMIT = 5


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


class GetDeploymentInput(BaseModel):
    """Inputs for ``get_deployment``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    namespace: str | None = None


@register_tool(
    name="get_deployment",
    description=(
        "Get full deployment state with rollout history. Includes all replica "
        "counts (desired/ready/available/updated/unavailable), strategy, "
        "selector, the full container list with images, conditions with "
        "transition age, and the last 5 ReplicaSets (rollout history) owned "
        "by this deployment. namespace='all' is rejected — specify a single "
        "namespace."
    ),
    input_model=GetDeploymentInput,
)
async def get_deployment(
    inp: GetDeploymentInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Get full deployment state with rollout history (last 5 revisions)."""
    if inp.namespace == "all":
        return ToolResult(
            success=False,
            error=(
                "namespace='all' is not supported for get_deployment; specify a single namespace"
            ),
        )

    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    assert targets is not None and len(targets) == 1
    namespace = targets[0]

    api = AppsV1Api(ctx.api_client)
    try:
        deployment = await asyncio.to_thread(
            api.read_namespaced_deployment, name=inp.name, namespace=namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            return ToolResult(
                success=False,
                error=f"deployment '{inp.name}' not found in namespace '{namespace}'",
            )
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("get_deployment failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    history = await _fetch_replica_sets(api, deployment=deployment, namespace=namespace)
    return ToolResult(success=True, data=_format_deployment_detail(deployment, history))


async def _fetch_replica_sets(
    api: AppsV1Api, *, deployment: Any, namespace: str
) -> list[dict[str, Any]]:
    """Fetch ReplicaSets owned by ``deployment`` and return revision-sorted summaries.

    Uses ``spec.selector.match_labels`` for the API-side label filter, then
    narrows client-side to ReplicaSets whose ``owner_references`` include the
    deployment's UID and ``kind="Deployment"``. UID is canonical here (unlike
    the events case): owner_references are populated by the controller.
    """
    metadata = getattr(deployment, "metadata", None)
    spec = getattr(deployment, "spec", None)
    selector = getattr(spec, "selector", None) if spec else None
    match_labels = (getattr(selector, "match_labels", None) or {}) if selector else {}
    if not match_labels:
        return []

    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    name = getattr(metadata, "name", None) if metadata else None
    try:
        result = await asyncio.to_thread(
            api.list_namespaced_replica_set,
            namespace=namespace,
            label_selector=label_selector,
        )
    except ApiException:
        logger.warning("get_deployment: failed to fetch replicasets for %s/%s", namespace, name)
        return []
    except Exception:
        logger.exception(
            "get_deployment: unexpected error fetching replicasets for %s/%s",
            namespace,
            name,
        )
        return []

    deployment_uid = getattr(metadata, "uid", None) if metadata else None
    owned = [rs for rs in result.items if _is_owned_by(rs, deployment_uid=deployment_uid)]
    owned.sort(key=_revision_of, reverse=True)
    return [_format_replicaset(rs) for rs in owned[:_DEPLOYMENT_REVISION_LIMIT]]


def _is_owned_by(rs: Any, *, deployment_uid: str | None) -> bool:
    if deployment_uid is None:
        return False
    metadata = getattr(rs, "metadata", None)
    owners = (getattr(metadata, "owner_references", None) or []) if metadata else []
    return any(
        getattr(o, "uid", None) == deployment_uid and getattr(o, "kind", None) == "Deployment"
        for o in owners
    )


def _revision_of(rs: Any) -> int:
    """Parse the ``deployment.kubernetes.io/revision`` annotation as int; -1 if missing."""
    metadata = getattr(rs, "metadata", None)
    annotations = (getattr(metadata, "annotations", None) or {}) if metadata else {}
    raw = annotations.get("deployment.kubernetes.io/revision")
    if raw is None:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _format_deployment_detail(deployment: Any, history: list[dict[str, Any]]) -> dict[str, Any]:
    metadata = getattr(deployment, "metadata", None)
    spec = getattr(deployment, "spec", None)
    status = getattr(deployment, "status", None)

    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    strategy = getattr(spec, "strategy", None) if spec else None
    strategy_type = getattr(strategy, "type", None) if strategy else None

    selector = getattr(spec, "selector", None) if spec else None
    match_labels = (getattr(selector, "match_labels", None) or {}) if selector else {}

    template = getattr(spec, "template", None) if spec else None
    pod_spec = getattr(template, "spec", None) if template else None
    containers = (getattr(pod_spec, "containers", None) or []) if pod_spec else []

    conditions = (getattr(status, "conditions", None) or []) if status else []

    return {
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "namespace": (getattr(metadata, "namespace", None) if metadata else None) or "Unknown",
        "age_seconds": secs,
        "age_human": age_human(secs),
        "strategy": strategy_type,
        "selector": dict(match_labels),
        "replicas_desired": getattr(spec, "replicas", None) if spec else None,
        "replicas_ready": (getattr(status, "ready_replicas", 0) or 0) if status else 0,
        "replicas_available": ((getattr(status, "available_replicas", 0) or 0) if status else 0),
        "replicas_updated": (getattr(status, "updated_replicas", 0) or 0) if status else 0,
        "replicas_unavailable": (
            (getattr(status, "unavailable_replicas", 0) or 0) if status else 0
        ),
        "containers": [_container_summary(c) for c in containers],
        "conditions": [format_condition(c) for c in conditions],
        "rollout_history": history,
    }


def _format_replicaset(rs: Any) -> dict[str, Any]:
    metadata = getattr(rs, "metadata", None)
    spec = getattr(rs, "spec", None)
    status = getattr(rs, "status", None)

    creation = getattr(metadata, "creation_timestamp", None) if metadata else None
    secs = age_seconds_since(creation)

    annotations = (getattr(metadata, "annotations", None) or {}) if metadata else {}
    revision_raw = annotations.get("deployment.kubernetes.io/revision")
    try:
        revision = int(revision_raw) if revision_raw is not None else None
    except (TypeError, ValueError):
        revision = None
    change_cause = annotations.get("kubernetes.io/change-cause")

    template = getattr(spec, "template", None) if spec else None
    pod_spec = getattr(template, "spec", None) if template else None
    containers = (getattr(pod_spec, "containers", None) or []) if pod_spec else []

    return {
        "revision": revision,
        "name": (getattr(metadata, "name", None) if metadata else None) or "Unknown",
        "replicas_desired": getattr(spec, "replicas", None) if spec else None,
        "replicas_ready": (getattr(status, "ready_replicas", 0) or 0) if status else 0,
        "age_seconds": secs,
        "age_human": age_human(secs),
        "change_cause": change_cause,
        "containers": [_container_summary(c) for c in containers],
    }


def _container_summary(c: Any) -> dict[str, Any]:
    return {"name": getattr(c, "name", None), "image": getattr(c, "image", None)}


# ===========================================================================
# scale_deployment (write tool — see CLAUDE.md §6.1)
# ===========================================================================


class ScaleDeploymentInput(BaseModel):
    """Inputs for ``scale_deployment``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    namespace: str | None = None
    replicas: int = Field(..., ge=0, le=1000)
    dry_run: bool = True


@register_tool(
    name="scale_deployment",
    description=(
        "Set the replica count of a deployment via the /scale sub-resource. "
        "dry_run=True (default) validates the patch with the K8s API "
        "(server-side dry-run) without applying. namespace='all' is rejected "
        "— specify a single namespace. Returns an audit dict capturing "
        "replicas_from / replicas_to / dry_run."
    ),
    input_model=ScaleDeploymentInput,
    is_write=True,
)
async def scale_deployment(
    inp: ScaleDeploymentInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Set the replica count of a deployment.

    See CLAUDE.md §6.1 for the Write Tool Contract this implements.
    """
    if (denied := assert_writes_enabled(settings)) is not None:
        return denied  # Layer 3

    if inp.namespace == "all":
        return ToolResult(
            success=False,
            error=(
                "namespace='all' is not supported for scale_deployment; specify a single namespace"
            ),
        )

    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    # After the "all" guard above, the resolver always returns a single-element list.
    assert targets is not None and len(targets) == 1
    namespace = targets[0]

    api = AppsV1Api(ctx.api_client)

    # 1. Read current replicas for the audit envelope.
    try:
        current = await asyncio.to_thread(
            api.read_namespaced_deployment, name=inp.name, namespace=namespace
        )
    except ApiException as exc:
        if exc.status == 404:
            return ToolResult(
                success=False,
                error=f"deployment '{inp.name}' not found in namespace '{namespace}'",
            )
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
        )
    except Exception as exc:
        logger.exception("scale_deployment read failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}")

    replicas_from = _current_replicas(current)
    audit: dict[str, Any] = {
        "namespace": namespace,
        "name": inp.name,
        "replicas_from": replicas_from,
        "replicas_to": inp.replicas,
        "dry_run": inp.dry_run,
    }

    # 2. Audit BEFORE attempting the patch — failed patches still get audited.
    log_write_operation("scale_deployment", **audit)

    # 3. Patch the /scale sub-resource. dry_run="All" → server-side validate-only.
    patch_kwargs: dict[str, Any] = {}
    if inp.dry_run:
        patch_kwargs["dry_run"] = "All"
    try:
        await asyncio.to_thread(
            api.patch_namespaced_deployment_scale,
            name=inp.name,
            namespace=namespace,
            body={"spec": {"replicas": inp.replicas}},
            **patch_kwargs,
        )
    except ApiException as exc:
        if exc.status == 404:
            # Race: deployment deleted between read and patch. Same friendly
            # error as 404-on-read so the LLM sees diagnostic equivalence.
            return ToolResult(
                success=False,
                error=f"deployment '{inp.name}' not found in namespace '{namespace}'",
                audit=audit,
            )
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
            audit=audit,
        )
    except Exception as exc:
        logger.exception("scale_deployment patch failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}", audit=audit)

    return ToolResult(
        success=True,
        data={
            "namespace": namespace,
            "name": inp.name,
            "replicas_from": replicas_from,
            "replicas_to": inp.replicas,
            "dry_run": inp.dry_run,
            "applied": not inp.dry_run,
        },
        audit=audit,
    )


def _current_replicas(deployment: Any) -> int | None:
    """Extract ``spec.replicas`` from a V1Deployment, passing ``None`` through.

    K8s defaults missing replicas to 1 at create time, so this should rarely
    be None in practice. We pass through rather than translate (consistent
    with ``list_deployments``); the audit log will surface ``None`` honestly
    in the rare malformed case.
    """
    spec = getattr(deployment, "spec", None)
    return getattr(spec, "replicas", None) if spec else None


# ===========================================================================
# restart_deployment (write tool — see CLAUDE.md §6.1)
# ===========================================================================


class RestartDeploymentInput(BaseModel):
    """Inputs for ``restart_deployment``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    namespace: str | None = None
    dry_run: bool = True


@register_tool(
    name="restart_deployment",
    description=(
        "Trigger a rolling restart of a deployment (equivalent to "
        "'kubectl rollout restart deployment/<name>'). Patches the pod "
        "template with a 'kubectl.kubernetes.io/restartedAt' annotation, "
        "which changes the template hash and causes the deployment "
        "controller to spin up a new ReplicaSet. dry_run=True (default) "
        "validates the patch without applying. namespace='all' is rejected."
    ),
    input_model=RestartDeploymentInput,
    is_write=True,
)
async def restart_deployment(
    inp: RestartDeploymentInput,
    *,
    ctx: KubeContext,
    settings: Settings,
) -> ToolResult:
    """Trigger a rolling restart by stamping the pod template annotation.

    See CLAUDE.md §6.1 for the Write Tool Contract this implements. Unlike
    ``scale_deployment``, no read-before-patch — restart has no "from" state
    worth capturing, so audit is always emitted before the single patch.
    """
    if (denied := assert_writes_enabled(settings)) is not None:
        return denied  # Layer 3

    if inp.namespace == "all":
        return ToolResult(
            success=False,
            error=(
                "namespace='all' is not supported for restart_deployment; "
                "specify a single namespace"
            ),
        )

    try:
        targets = resolve_read_namespaces(inp.namespace, settings=settings, ctx=ctx)
    except NamespaceNotAllowedError as exc:
        return ToolResult(success=False, error=str(exc))

    assert targets is not None and len(targets) == 1
    namespace = targets[0]

    # Generate ONCE — same string flows to body, audit, and response.
    # RFC3339 with "Z" suffix matches kubectl rollout restart byte-for-byte,
    # so external tools (Argo CD, Flux, observability dashboards) that parse
    # the annotation find our restarts too.
    restarted_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    audit: dict[str, Any] = {
        "namespace": namespace,
        "name": inp.name,
        "restarted_at": restarted_at,
        "dry_run": inp.dry_run,
    }
    log_write_operation("restart_deployment", **audit)

    api = AppsV1Api(ctx.api_client)
    patch_kwargs: dict[str, Any] = {}
    if inp.dry_run:
        patch_kwargs["dry_run"] = "All"
    try:
        await asyncio.to_thread(
            api.patch_namespaced_deployment,
            name=inp.name,
            namespace=namespace,
            body=_restart_patch_body(restarted_at),
            **patch_kwargs,
        )
    except ApiException as exc:
        if exc.status == 404:
            return ToolResult(
                success=False,
                error=f"deployment '{inp.name}' not found in namespace '{namespace}'",
                audit=audit,
            )
        return ToolResult(
            success=False,
            error=f"kubernetes API error: {exc.reason or exc.status}",
            audit=audit,
        )
    except Exception as exc:
        logger.exception("restart_deployment patch failed")
        return ToolResult(success=False, error=f"unexpected error: {exc}", audit=audit)

    return ToolResult(
        success=True,
        data={
            "namespace": namespace,
            "name": inp.name,
            "restarted_at": restarted_at,
            "dry_run": inp.dry_run,
            "applied": not inp.dry_run,
        },
        audit=audit,
    )


def _restart_patch_body(restarted_at: str) -> dict[str, Any]:
    """Build the deep-nested JSON-merge patch body that kubectl rollout restart uses.

    Patching ``spec.template.metadata.annotations`` mutates the pod template,
    which changes the template hash and triggers the deployment controller to
    spin up a new ReplicaSet — exactly what ``kubectl rollout restart`` does.
    The annotation key ``kubectl.kubernetes.io/restartedAt`` is kubectl's
    canonical key; using the same key means external tools that look for
    restart history find ours too.
    """
    return {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": restarted_at,
                    }
                }
            }
        }
    }
