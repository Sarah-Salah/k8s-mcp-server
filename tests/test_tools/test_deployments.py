"""Tests for the ``list_deployments`` tool."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools._registry import ToolResult
from k8s_mcp_server.tools.deployments import (
    GetDeploymentInput,
    ListDeploymentsInput,
    RestartDeploymentInput,
    ScaleDeploymentInput,
    get_deployment,
    list_deployments,
    restart_deployment,
    scale_deployment,
)

PATCH_TARGET = "k8s_mcp_server.tools.deployments"


def _container(name: str, *, image: str | None = "nginx:1.25") -> SimpleNamespace:
    return SimpleNamespace(name=name, image=image)


def _deployment(
    name: str,
    *,
    namespace: str = "default",
    replicas_desired: int | None = 3,
    ready_replicas: int | None = 3,
    age_minutes: int = 60,
    containers: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace, creation_timestamp=created),
        spec=SimpleNamespace(
            replicas=replicas_desired,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=containers if containers is not None else [_container("app")]
                )
            ),
        ),
        status=SimpleNamespace(ready_replicas=ready_replicas),
    )


@pytest.fixture
def deployments_api(patch_apps_v1: Callable[[str], MagicMock]) -> MagicMock:
    return patch_apps_v1(PATCH_TARGET)


# ---------------------------------------------------------------------------
# Namespace dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specific_namespace_calls_list_namespaced_deployment(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[_deployment("api", namespace="dev")]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    deployments_api.list_namespaced_deployment.assert_called_once()
    assert deployments_api.list_namespaced_deployment.call_args.kwargs["namespace"] == "dev"
    deployments_api.list_deployment_for_all_namespaces.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_none_uses_context_default(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(items=[])

    await list_deployments(ListDeploymentsInput(), ctx=kube_context, settings=Settings())

    assert deployments_api.list_namespaced_deployment.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_all_no_allowlist_calls_list_deployment_for_all_namespaces(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_deployment_for_all_namespaces.return_value = SimpleNamespace(
        items=[_deployment("api"), _deployment("web", namespace="prod")]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="all"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    deployments_api.list_deployment_for_all_namespaces.assert_called_once()
    deployments_api.list_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_all_with_allowlist_iterates_allowlisted_namespaces(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.side_effect = [
        SimpleNamespace(items=[_deployment("dev-app", namespace="dev")]),
        SimpleNamespace(items=[_deployment("staging-app", namespace="staging")]),
    ]

    result = await list_deployments(
        ListDeploymentsInput(namespace="all"),
        ctx=kube_context,
        settings=Settings(namespaces=("staging", "dev")),
    )

    assert result.success is True
    deployments_api.list_deployment_for_all_namespaces.assert_not_called()
    called_namespaces = [
        call.kwargs["namespace"]
        for call in deployments_api.list_namespaced_deployment.call_args_list
    ]
    assert called_namespaces == ["dev", "staging"]
    names = [d["name"] for d in result.data["deployments"]]
    assert names == ["dev-app", "staging-app"]


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await list_deployments(
        ListDeploymentsInput(namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")
    deployments_api.list_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await list_deployments(
        ListDeploymentsInput(),
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


# ---------------------------------------------------------------------------
# Selectors / truncation / sorting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_selector_passed_to_api(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(items=[])

    await list_deployments(
        ListDeploymentsInput(namespace="dev", label_selector="app=api,tier=backend"),
        ctx=kube_context,
        settings=Settings(),
    )

    kwargs = deployments_api.list_namespaced_deployment.call_args.kwargs
    assert kwargs["label_selector"] == "app=api,tier=backend"


@pytest.mark.asyncio
async def test_truncated_true_when_results_exceed_limit(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_deployment_for_all_namespaces.return_value = SimpleNamespace(
        items=[_deployment(f"d{i}") for i in range(8)]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="all", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["truncated"] is True
    assert len(result.data["deployments"]) == 5


@pytest.mark.asyncio
async def test_truncated_false_when_results_fit_in_limit(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_deployment_for_all_namespaces.return_value = SimpleNamespace(
        items=[_deployment(f"d{i}") for i in range(2)]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="all", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is False
    assert len(result.data["deployments"]) == 2


@pytest.mark.asyncio
async def test_deployments_sorted_by_namespace_then_name(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_deployment_for_all_namespaces.return_value = SimpleNamespace(
        items=[
            _deployment("zeta", namespace="dev"),
            _deployment("alpha", namespace="prod"),
            _deployment("beta", namespace="dev"),
        ]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="all"), ctx=kube_context, settings=Settings()
    )

    sequence = [(d["namespace"], d["name"]) for d in result.data["deployments"]]
    assert sequence == [("dev", "beta"), ("dev", "zeta"), ("prod", "alpha")]


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_format_includes_all_required_fields(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[
            _deployment(
                "api",
                namespace="staging",
                replicas_desired=5,
                ready_replicas=3,
                age_minutes=180,
                containers=[_container("app", image="api:1.4")],
            )
        ]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="staging"), ctx=kube_context, settings=Settings()
    )

    [out] = result.data["deployments"]
    assert out["name"] == "api"
    assert out["namespace"] == "staging"
    assert out["replicas_desired"] == 5
    assert out["replicas_ready"] == 3
    assert out["age_seconds"] >= 180 * 60
    assert "h" in out["age_human"]
    assert out["image"] == "api:1.4"


@pytest.mark.asyncio
async def test_replicas_desired_passes_none_through(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """spec.replicas can be None — pass through, don't translate to 1."""
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[_deployment("api", replicas_desired=None)]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["deployments"][0]["replicas_desired"] is None


@pytest.mark.asyncio
async def test_replicas_ready_coerces_none_to_zero(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """status.ready_replicas can be None when no pods are ready — coerce to 0."""
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[_deployment("api", ready_replicas=None)]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["deployments"][0]["replicas_ready"] == 0


@pytest.mark.asyncio
async def test_image_is_first_container_when_multiple(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[
            _deployment(
                "api",
                containers=[
                    _container("app", image="api:1.4"),
                    _container("sidecar", image="proxy:0.9"),
                ],
            )
        ]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["deployments"][0]["image"] == "api:1.4"


@pytest.mark.asyncio
async def test_image_is_none_when_no_containers(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(
        items=[_deployment("api", containers=[])]
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["deployments"][0]["image"] is None


@pytest.mark.asyncio
async def test_with_missing_metadata_spec_status_does_not_crash(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    weird: Any = SimpleNamespace(metadata=None, spec=None, status=None)
    deployments_api.list_namespaced_deployment.return_value = SimpleNamespace(items=[weird])

    result = await list_deployments(
        ListDeploymentsInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    [out] = result.data["deployments"]
    assert out["name"] == "Unknown"
    assert out["namespace"] == "Unknown"
    assert out["replicas_desired"] is None
    assert out["replicas_ready"] == 0
    assert out["image"] is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_error_on_api_exception(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.side_effect = ApiException(
        status=403, reason="Forbidden"
    )

    result = await list_deployments(
        ListDeploymentsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "Forbidden" in (result.error or "")


@pytest.mark.asyncio
async def test_returns_error_on_unexpected_exception(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.list_namespaced_deployment.side_effect = RuntimeError("boom")

    result = await list_deployments(
        ListDeploymentsInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": "nope"},
        {"limit": 0},
        {"limit": -1},
        {"limit": 1001},
    ],
)
def test_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ListDeploymentsInput.model_validate(payload)


# ===========================================================================
# get_deployment
# ===========================================================================


def _owner_ref(*, uid: str, kind: str = "Deployment") -> SimpleNamespace:
    return SimpleNamespace(uid=uid, kind=kind)


def _condition(
    type_: str,
    status: str,
    *,
    reason: str | None = None,
    message: str | None = None,
    age_minutes: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=type_,
        status=status,
        reason=reason,
        message=message,
        last_transition_time=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


def _detailed_deployment(
    name: str = "api",
    *,
    namespace: str = "staging",
    uid: str = "dep-uid-1",
    match_labels: dict[str, str] | None = None,
    replicas_desired: int | None = 5,
    ready_replicas: int | None = 5,
    available_replicas: int | None = 5,
    updated_replicas: int | None = 5,
    unavailable_replicas: int | None = 0,
    strategy: str = "RollingUpdate",
    age_minutes: int = 120,
    containers: list[SimpleNamespace] | None = None,
    conditions: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace=namespace, uid=uid, creation_timestamp=created
        ),
        spec=SimpleNamespace(
            replicas=replicas_desired,
            strategy=SimpleNamespace(type=strategy),
            selector=SimpleNamespace(
                match_labels={"app": name} if match_labels is None else match_labels
            ),
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=containers if containers is not None else [_container("app")]
                )
            ),
        ),
        status=SimpleNamespace(
            ready_replicas=ready_replicas,
            available_replicas=available_replicas,
            updated_replicas=updated_replicas,
            unavailable_replicas=unavailable_replicas,
            conditions=conditions,
        ),
    )


def _replicaset(
    name: str,
    *,
    revision: str | None = "1",
    change_cause: str | None = None,
    owner_uid: str | None = "dep-uid-1",
    owner_kind: str = "Deployment",
    replicas_desired: int | None = 3,
    ready_replicas: int | None = 3,
    age_minutes: int = 60,
    containers: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    annotations: dict[str, str] = {}
    if revision is not None:
        annotations["deployment.kubernetes.io/revision"] = revision
    if change_cause is not None:
        annotations["kubernetes.io/change-cause"] = change_cause
    owner_refs = [_owner_ref(uid=owner_uid, kind=owner_kind)] if owner_uid else []
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            owner_references=owner_refs,
            annotations=annotations,
            creation_timestamp=created,
        ),
        spec=SimpleNamespace(
            replicas=replicas_desired,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=containers if containers is not None else [_container("app")]
                )
            ),
        ),
        status=SimpleNamespace(ready_replicas=ready_replicas),
    )


@pytest.mark.asyncio
async def test_get_deployment_returns_full_state(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(
        name="api",
        namespace="staging",
        match_labels={"app": "api"},
        containers=[
            _container("app", image="api:1.4"),
            _container("sidecar", image="proxy:0.9"),
        ],
        conditions=[_condition("Available", "True", age_minutes=100)],
    )
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[
            _replicaset("api-5", revision="5", change_cause="apply", age_minutes=10),
            _replicaset("api-4", revision="4", age_minutes=120),
        ]
    )

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    data = result.data
    assert data["name"] == "api"
    assert data["namespace"] == "staging"
    assert data["strategy"] == "RollingUpdate"
    assert data["selector"] == {"app": "api"}
    assert data["replicas_desired"] == 5
    assert data["replicas_ready"] == 5
    assert data["replicas_available"] == 5
    assert data["replicas_updated"] == 5
    assert data["replicas_unavailable"] == 0
    assert data["containers"] == [
        {"name": "app", "image": "api:1.4"},
        {"name": "sidecar", "image": "proxy:0.9"},
    ]
    assert len(data["conditions"]) == 1
    assert data["conditions"][0]["type"] == "Available"
    assert data["conditions"][0]["last_transition_age_seconds"] >= 100 * 60
    assert len(data["rollout_history"]) == 2
    assert data["rollout_history"][0]["revision"] == 5
    assert data["rollout_history"][0]["change_cause"] == "apply"
    assert data["rollout_history"][1]["revision"] == 4


@pytest.mark.asyncio
async def test_get_deployment_uses_default_namespace_when_none(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(
        namespace="default"
    )
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

    await get_deployment(GetDeploymentInput(name="api"), ctx=kube_context, settings=Settings())

    assert deployments_api.read_namespaced_deployment.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_get_deployment_specific_namespace_passes_through(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(namespace="dev")
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

    await get_deployment(
        GetDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert deployments_api.read_namespaced_deployment.call_args.kwargs["namespace"] == "dev"


@pytest.mark.asyncio
async def test_get_deployment_rejects_namespace_all(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="all"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "single namespace" in (result.error or "")
    deployments_api.read_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_get_deployment_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")


@pytest.mark.asyncio
async def test_get_deployment_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await get_deployment(
        GetDeploymentInput(name="api"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


@pytest.mark.asyncio
async def test_get_deployment_404_returns_friendly_not_found_error(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    result = await get_deployment(
        GetDeploymentInput(name="ghost", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "ghost" in (result.error or "")
    assert "staging" in (result.error or "")
    assert "not found" in (result.error or "")
    deployments_api.list_namespaced_replica_set.assert_not_called()


@pytest.mark.asyncio
async def test_get_deployment_non_404_api_error_returns_kubernetes_api_error(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Internal"
    )

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")


@pytest.mark.asyncio
async def test_get_deployment_unexpected_exception_returns_error(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.side_effect = RuntimeError("boom")

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_get_deployment_replicaset_label_selector_built_from_match_labels(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(
        match_labels={"app": "api", "tier": "backend"}
    )
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

    await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    kwargs = deployments_api.list_namespaced_replica_set.call_args.kwargs
    assert kwargs["namespace"] == "staging"
    # Order isn't guaranteed across dicts but both labels must be present.
    parts = set(kwargs["label_selector"].split(","))
    assert parts == {"app=api", "tier=backend"}


@pytest.mark.asyncio
async def test_get_deployment_replicasets_filtered_by_owner_uid_and_kind(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Only RSs owned by THIS deployment (matching uid + kind=Deployment) are kept."""
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(uid="dep-uid-1")
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[
            _replicaset("ours-1", revision="2", owner_uid="dep-uid-1"),
            _replicaset("theirs", revision="9", owner_uid="other-dep-uid"),
            _replicaset(
                "wrong-kind",
                revision="9",
                owner_uid="dep-uid-1",
                owner_kind="StatefulSet",
            ),
            _replicaset("ours-2", revision="3", owner_uid="dep-uid-1"),
        ]
    )

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    names = [rs["name"] for rs in result.data["rollout_history"]]
    assert names == ["ours-2", "ours-1"]  # rev 3 then rev 2


@pytest.mark.asyncio
async def test_get_deployment_replicasets_sorted_by_revision_desc_capped_at_5(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(uid="dep-uid-1")
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[_replicaset(f"rs-{i}", revision=str(i), owner_uid="dep-uid-1") for i in range(1, 9)]
    )

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    history = result.data["rollout_history"]
    assert len(history) == 5
    assert [rs["revision"] for rs in history] == [8, 7, 6, 5, 4]


@pytest.mark.asyncio
async def test_get_deployment_replicaset_with_no_revision_annotation_sorts_to_bottom(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(uid="dep-uid-1")
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[
            _replicaset("no-rev", revision=None, owner_uid="dep-uid-1"),
            _replicaset("rev-2", revision="2", owner_uid="dep-uid-1"),
            _replicaset("bad-rev", revision="not-an-int", owner_uid="dep-uid-1"),
        ]
    )

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    names = [rs["name"] for rs in result.data["rollout_history"]]
    # rev=2 first, then the two with revision=None (both sort to bottom)
    assert names[0] == "rev-2"
    assert set(names[1:]) == {"no-rev", "bad-rev"}
    assert result.data["rollout_history"][0]["revision"] == 2
    # The two no-revision entries surface revision=None in the output
    no_rev_entries = [rs for rs in result.data["rollout_history"] if rs["revision"] is None]
    assert len(no_rev_entries) == 2


@pytest.mark.asyncio
async def test_get_deployment_replicaset_fetch_failure_returns_empty_history_with_deployment_data(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(name="api")
    deployments_api.list_namespaced_replica_set.side_effect = ApiException(
        status=403, reason="Forbidden"
    )

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.error is None
    assert result.data["name"] == "api"
    assert result.data["rollout_history"] == []


@pytest.mark.asyncio
async def test_get_deployment_replicaset_fetch_unexpected_exception_returns_empty_history(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment()
    deployments_api.list_namespaced_replica_set.side_effect = RuntimeError("rs boom")

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["rollout_history"] == []


@pytest.mark.asyncio
async def test_get_deployment_no_match_labels_skips_replicaset_call(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Defensive: deployment with no spec.selector.match_labels → no RS API call."""
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(match_labels={})

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["rollout_history"] == []
    deployments_api.list_namespaced_replica_set.assert_not_called()


@pytest.mark.asyncio
async def test_get_deployment_full_container_list_included(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(
        containers=[
            _container("app", image="api:1.4"),
            _container("sidecar", image="proxy:0.9"),
            _container("log-shipper", image="fluentbit:2.1"),
        ]
    )
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["containers"] == [
        {"name": "app", "image": "api:1.4"},
        {"name": "sidecar", "image": "proxy:0.9"},
        {"name": "log-shipper", "image": "fluentbit:2.1"},
    ]


@pytest.mark.asyncio
async def test_get_deployment_replicas_status_fields_coerce_none_to_zero(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """available_replicas / updated_replicas / unavailable_replicas can be None."""
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(
        available_replicas=None,
        updated_replicas=None,
        unavailable_replicas=None,
    )
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(items=[])

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["replicas_available"] == 0
    assert result.data["replicas_updated"] == 0
    assert result.data["replicas_unavailable"] == 0


@pytest.mark.asyncio
async def test_get_deployment_with_missing_metadata_spec_status_does_not_crash(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    weird: Any = SimpleNamespace(metadata=None, spec=None, status=None)
    deployments_api.read_namespaced_deployment.return_value = weird

    result = await get_deployment(
        GetDeploymentInput(name="x", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    data = result.data
    assert data["name"] == "Unknown"
    assert data["namespace"] == "Unknown"
    assert data["strategy"] is None
    assert data["selector"] == {}
    assert data["replicas_desired"] is None
    assert data["replicas_ready"] == 0
    assert data["replicas_available"] == 0
    assert data["containers"] == []
    assert data["conditions"] == []
    assert data["rollout_history"] == []
    # Defensive: no RS call when there's no selector
    deployments_api.list_namespaced_replica_set.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing name
        {"name": ""},  # empty name
        {"name": "x", "extra": "nope"},  # extra field
    ],
)
def test_get_deployment_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GetDeploymentInput.model_validate(payload)


@pytest.mark.asyncio
async def test_get_deployment_with_no_uid_filters_out_all_replicasets(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Defensive: deployment with metadata.uid=None can't match any owner
    reference, so the rollout history comes back empty even when the API
    returns ReplicaSets."""
    deployments_api.read_namespaced_deployment.return_value = _detailed_deployment(
        uid=None  # type: ignore[arg-type]
    )
    deployments_api.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[_replicaset("orphan", revision="1", owner_uid="some-other-uid")]
    )

    result = await get_deployment(
        GetDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["rollout_history"] == []


# ===========================================================================
# scale_deployment
# ===========================================================================


_WRITES_ON = Settings(enable_writes=True)


def _deployment_with_replicas(replicas: int | None = 3) -> SimpleNamespace:
    """Minimal V1Deployment-shaped object that read_namespaced_deployment returns."""
    return SimpleNamespace(spec=SimpleNamespace(replicas=replicas))


# ---------------------------------------------------------------------------
# §6.1 Layer 3 enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_writes_disabled_returns_layer3_error_before_any_api_call(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Layer 3: with enable_writes=False, NO K8s call should happen."""
    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5),
        ctx=kube_context,
        settings=Settings(enable_writes=False),
    )

    assert result.success is False
    assert result.error == (
        "write operations are disabled; restart the server with --enable-writes to enable"
    )
    deployments_api.read_namespaced_deployment.assert_not_called()
    deployments_api.patch_namespaced_deployment_scale.assert_not_called()


# ---------------------------------------------------------------------------
# Namespace handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_rejects_namespace_all(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="all", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "single namespace" in (result.error or "")
    deployments_api.read_namespaced_deployment.assert_not_called()
    deployments_api.patch_namespaced_deployment_scale.assert_not_called()


@pytest.mark.asyncio
async def test_scale_uses_default_namespace_when_none(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)

    await scale_deployment(
        ScaleDeploymentInput(name="api", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert deployments_api.read_namespaced_deployment.call_args.kwargs["namespace"] == "default"
    assert (
        deployments_api.patch_namespaced_deployment_scale.call_args.kwargs["namespace"] == "default"
    )


@pytest.mark.asyncio
async def test_scale_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="prod", replicas=5),
        ctx=kube_context,
        settings=Settings(enable_writes=True, namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")
    deployments_api.read_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_scale_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await scale_deployment(
        ScaleDeploymentInput(name="api", replicas=5),
        ctx=kube_context,
        settings=Settings(enable_writes=True, namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


# ---------------------------------------------------------------------------
# dry_run semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_dry_run_true_passes_dry_run_all_to_api(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5, dry_run=True),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is True
    kwargs = deployments_api.patch_namespaced_deployment_scale.call_args.kwargs
    assert kwargs["dry_run"] == "All"
    assert result.data["dry_run"] is True
    assert result.data["applied"] is False


@pytest.mark.asyncio
async def test_scale_dry_run_false_omits_dry_run_kwarg_and_marks_applied(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5, dry_run=False),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is True
    kwargs = deployments_api.patch_namespaced_deployment_scale.call_args.kwargs
    assert "dry_run" not in kwargs
    assert result.data["dry_run"] is False
    assert result.data["applied"] is True


@pytest.mark.asyncio
async def test_scale_default_dry_run_is_true(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Layer 4: omitting dry_run defaults to True — not applied."""
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is True
    assert result.data["dry_run"] is True
    assert result.data["applied"] is False
    assert deployments_api.patch_namespaced_deployment_scale.call_args.kwargs["dry_run"] == "All"


# ---------------------------------------------------------------------------
# Patch body and replicas_from capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_replicas_from_captured_from_current_deployment(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(7)

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=10),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.data["replicas_from"] == 7
    assert result.data["replicas_to"] == 10


@pytest.mark.asyncio
async def test_scale_replicas_from_passes_none_through_when_unset(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """spec.replicas=None (malformed/unusual) passes through, not translated to 1."""
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(None)

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=3),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.data["replicas_from"] is None
    assert result.data["replicas_to"] == 3


@pytest.mark.asyncio
async def test_scale_patches_scale_subresource_with_correct_body(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Uses /scale (not /deployment) and sends the JSON-merge body."""
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)

    await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="staging", replicas=8),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    deployments_api.patch_namespaced_deployment_scale.assert_called_once()
    kwargs = deployments_api.patch_namespaced_deployment_scale.call_args.kwargs
    assert kwargs["name"] == "api"
    assert kwargs["namespace"] == "staging"
    assert kwargs["body"] == {"spec": {"replicas": 8}}
    # patch_namespaced_deployment (the wrong sub-resource) is NEVER called
    deployments_api.patch_namespaced_deployment.assert_not_called()


# ---------------------------------------------------------------------------
# Audit (log + ToolResult.audit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_audit_fields_present_in_log_and_envelope(
    kube_context: KubeContext,
    deployments_api: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)

    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        result = await scale_deployment(
            ScaleDeploymentInput(name="api", namespace="staging", replicas=5, dry_run=False),
            ctx=kube_context,
            settings=_WRITES_ON,
        )

    expected_audit = {
        "namespace": "staging",
        "name": "api",
        "replicas_from": 3,
        "replicas_to": 5,
        "dry_run": False,
    }
    assert result.audit == expected_audit

    [record] = caplog.records
    assert record.name == "k8s_mcp_server.audit"
    assert "write_operation tool=scale_deployment" in record.message
    assert "namespace=staging" in record.message
    assert "name=api" in record.message
    assert "replicas_from=3" in record.message
    assert "replicas_to=5" in record.message
    assert "dry_run=False" in record.message


@pytest.mark.asyncio
async def test_scale_audit_present_on_failed_patch_path(
    kube_context: KubeContext,
    deployments_api: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failed PATCH still gets audited — that's the whole point of pre-attempt logging."""
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)
    deployments_api.patch_namespaced_deployment_scale.side_effect = ApiException(
        status=500, reason="Internal"
    )

    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        result = await scale_deployment(
            ScaleDeploymentInput(name="api", namespace="dev", replicas=5),
            ctx=kube_context,
            settings=_WRITES_ON,
        )

    assert result.success is False
    assert result.audit is not None
    assert result.audit["replicas_from"] == 3
    assert result.audit["replicas_to"] == 5
    # Audit log line still emitted (failed patch is still an attempt)
    assert any("write_operation tool=scale_deployment" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_scale_no_audit_on_failed_read_path(
    kube_context: KubeContext,
    deployments_api: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failed READ means the operation wasn't attempted; no audit log, no audit envelope."""
    deployments_api.read_namespaced_deployment.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        result = await scale_deployment(
            ScaleDeploymentInput(name="ghost", namespace="dev", replicas=5),
            ctx=kube_context,
            settings=_WRITES_ON,
        )

    assert result.success is False
    assert result.audit is None
    assert not any("write_operation" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 404 errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_404_on_read_returns_friendly_error(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    result = await scale_deployment(
        ScaleDeploymentInput(name="ghost", namespace="staging", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert result.error == "deployment 'ghost' not found in namespace 'staging'"
    deployments_api.patch_namespaced_deployment_scale.assert_not_called()


@pytest.mark.asyncio
async def test_scale_404_on_patch_race_returns_same_friendly_error(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Deployment deleted between read and patch: same diagnostic equivalence."""
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)
    deployments_api.patch_namespaced_deployment_scale.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="staging", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert result.error == "deployment 'api' not found in namespace 'staging'"
    # Race-loss audit is still recorded — we attempted
    assert result.audit is not None


# ---------------------------------------------------------------------------
# Other API errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_non_404_read_error_returns_kubernetes_api_error(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Internal"
    )

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")
    assert result.audit is None


@pytest.mark.asyncio
async def test_scale_non_404_patch_error_returns_kubernetes_api_error_with_audit(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)
    deployments_api.patch_namespaced_deployment_scale.side_effect = ApiException(
        status=500, reason="Internal"
    )

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert result.audit is not None


@pytest.mark.asyncio
async def test_scale_unexpected_exception_on_read(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.side_effect = RuntimeError("read boom")

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "read boom" in (result.error or "")
    assert result.audit is None


@pytest.mark.asyncio
async def test_scale_unexpected_exception_on_patch(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)
    deployments_api.patch_namespaced_deployment_scale.side_effect = RuntimeError("patch boom")

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=5),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "patch boom" in (result.error or "")
    assert result.audit is not None


# ---------------------------------------------------------------------------
# Boundary values / input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_replicas_zero_accepted(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Scale-down-to-zero is a valid operation (stop the deployment)."""
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(5)

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=0),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is True
    assert result.data["replicas_to"] == 0
    assert deployments_api.patch_namespaced_deployment_scale.call_args.kwargs["body"] == {
        "spec": {"replicas": 0}
    }


@pytest.mark.asyncio
async def test_scale_replicas_max_1000_accepted(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.read_namespaced_deployment.return_value = _deployment_with_replicas(3)

    result = await scale_deployment(
        ScaleDeploymentInput(name="api", namespace="dev", replicas=1000),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is True
    assert result.data["replicas_to"] == 1000


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "api", "replicas": -1},
        {"name": "api", "replicas": 1001},
        {"name": "api"},
        {"replicas": 5},
        {"name": "", "replicas": 5},
        {"name": "api", "replicas": 5, "extra": "x"},
    ],
)
def test_scale_input_validation_rejects_bad_payloads(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScaleDeploymentInput.model_validate(payload)


def test_scale_tool_is_marked_is_write_true() -> None:
    """Sanity: the tool MUST be registered with is_write=True so Layer 2 filters it."""
    from k8s_mcp_server.tools._registry import all_tools

    [scale] = [t for t in all_tools() if t.name == "scale_deployment"]
    assert scale.is_write is True

    # Silence "imported but unused" for the local-module ToolResult.
    _ = ToolResult


# ===========================================================================
# restart_deployment
# ===========================================================================


import re  # noqa: E402

_RFC3339_Z_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ---------------------------------------------------------------------------
# §6.1 Layer 3 enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_writes_disabled_returns_layer3_error_before_any_api_call(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(enable_writes=False),
    )

    assert result.success is False
    assert result.error == (
        "write operations are disabled; restart the server with --enable-writes to enable"
    )
    deployments_api.patch_namespaced_deployment.assert_not_called()


# ---------------------------------------------------------------------------
# Namespace handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_rejects_namespace_all(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="all"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "single namespace" in (result.error or "")
    deployments_api.patch_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_restart_uses_default_namespace_when_none(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    await restart_deployment(
        RestartDeploymentInput(name="api"), ctx=kube_context, settings=_WRITES_ON
    )

    kwargs = deployments_api.patch_namespaced_deployment.call_args.kwargs
    assert kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_restart_specific_namespace_passes_through(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    await restart_deployment(
        RestartDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert deployments_api.patch_namespaced_deployment.call_args.kwargs["namespace"] == "staging"


@pytest.mark.asyncio
async def test_restart_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="prod"),
        ctx=kube_context,
        settings=Settings(enable_writes=True, namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")
    deployments_api.patch_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_restart_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await restart_deployment(
        RestartDeploymentInput(name="api"),
        ctx=kube_context,
        settings=Settings(enable_writes=True, namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


# ---------------------------------------------------------------------------
# dry_run semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_dry_run_true_passes_dry_run_all_to_api(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev", dry_run=True),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is True
    assert deployments_api.patch_namespaced_deployment.call_args.kwargs["dry_run"] == "All"
    assert result.data["dry_run"] is True
    assert result.data["applied"] is False


@pytest.mark.asyncio
async def test_restart_dry_run_false_omits_kwarg_and_marks_applied(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev", dry_run=False),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is True
    assert "dry_run" not in deployments_api.patch_namespaced_deployment.call_args.kwargs
    assert result.data["dry_run"] is False
    assert result.data["applied"] is True


@pytest.mark.asyncio
async def test_restart_default_dry_run_is_true(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.data["dry_run"] is True
    assert result.data["applied"] is False
    assert deployments_api.patch_namespaced_deployment.call_args.kwargs["dry_run"] == "All"


# ---------------------------------------------------------------------------
# Patch body & resource selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_patch_body_has_correct_deep_nesting(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """The body MUST match kubectl rollout restart's exact shape, including
    the canonical annotation key."""
    await restart_deployment(
        RestartDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    body = deployments_api.patch_namespaced_deployment.call_args.kwargs["body"]
    # Exact deep-nesting check
    annotation_value = body["spec"]["template"]["metadata"]["annotations"][
        "kubectl.kubernetes.io/restartedAt"
    ]
    assert annotation_value  # non-empty
    # The body has exactly the keys we expect (no extra noise)
    assert body == {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": annotation_value,
                    }
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_restart_uses_main_deployment_resource_not_scale_subresource(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Restart mutates the pod template via the main resource, NOT /scale."""
    await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    deployments_api.patch_namespaced_deployment.assert_called_once()
    deployments_api.patch_namespaced_deployment_scale.assert_not_called()


@pytest.mark.asyncio
async def test_restart_no_read_before_patch(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Unlike scale_deployment, restart has no read-before-patch — there's
    no 'from' state worth capturing."""
    await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    deployments_api.read_namespaced_deployment.assert_not_called()


# ---------------------------------------------------------------------------
# Timestamp consistency and format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_timestamp_is_same_value_in_body_audit_and_response(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Generated once per call — must be byte-for-byte identical across all
    three sites so a downstream operator can correlate the audit log line
    with the actual annotation set on the deployment.
    """
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="staging"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    body = deployments_api.patch_namespaced_deployment.call_args.kwargs["body"]
    annotation_value = body["spec"]["template"]["metadata"]["annotations"][
        "kubectl.kubernetes.io/restartedAt"
    ]
    assert result.audit is not None
    assert result.audit["restarted_at"] == annotation_value
    assert result.data["restarted_at"] == annotation_value


@pytest.mark.asyncio
async def test_restart_timestamp_is_rfc3339_with_z_suffix(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    """Matches kubectl rollout restart's annotation format byte-for-byte —
    seconds precision, ``Z`` suffix (no ``+00:00`` form)."""
    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    timestamp = result.data["restarted_at"]
    assert _RFC3339_Z_PATTERN.match(timestamp) is not None, (
        f"Timestamp {timestamp!r} does not match RFC3339-Z (kubectl format)"
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_audit_fields_present_in_log_and_envelope(
    kube_context: KubeContext,
    deployments_api: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        result = await restart_deployment(
            RestartDeploymentInput(name="api", namespace="staging", dry_run=False),
            ctx=kube_context,
            settings=_WRITES_ON,
        )

    assert result.audit is not None
    assert result.audit["namespace"] == "staging"
    assert result.audit["name"] == "api"
    assert result.audit["dry_run"] is False
    assert _RFC3339_Z_PATTERN.match(result.audit["restarted_at"]) is not None

    [record] = caplog.records
    assert record.name == "k8s_mcp_server.audit"
    assert "write_operation tool=restart_deployment" in record.message
    assert "namespace=staging" in record.message
    assert "name=api" in record.message
    assert f"restarted_at={result.audit['restarted_at']}" in record.message
    assert "dry_run=False" in record.message


@pytest.mark.asyncio
async def test_restart_audit_present_on_failed_patch(
    kube_context: KubeContext,
    deployments_api: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No read-before-patch → audit is ALWAYS emitted before the patch
    attempt and ALWAYS present in the envelope (success or failure).
    """
    deployments_api.patch_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Internal"
    )

    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        result = await restart_deployment(
            RestartDeploymentInput(name="api", namespace="dev"),
            ctx=kube_context,
            settings=_WRITES_ON,
        )

    assert result.success is False
    assert result.audit is not None
    assert any("write_operation tool=restart_deployment" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_404_returns_friendly_error_with_audit(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.patch_namespaced_deployment.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    result = await restart_deployment(
        RestartDeploymentInput(name="ghost", namespace="staging"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert result.error == "deployment 'ghost' not found in namespace 'staging'"
    assert result.audit is not None  # audit happened before the patch


@pytest.mark.asyncio
async def test_restart_non_404_api_error_returns_kubernetes_api_error(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.patch_namespaced_deployment.side_effect = ApiException(
        status=500, reason="Internal"
    )

    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")
    assert result.audit is not None


@pytest.mark.asyncio
async def test_restart_unexpected_exception_returns_error_with_audit(
    kube_context: KubeContext, deployments_api: MagicMock
) -> None:
    deployments_api.patch_namespaced_deployment.side_effect = RuntimeError("boom")

    result = await restart_deployment(
        RestartDeploymentInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=_WRITES_ON,
    )

    assert result.success is False
    assert "boom" in (result.error or "")
    assert result.audit is not None


# ---------------------------------------------------------------------------
# Registration & input validation
# ---------------------------------------------------------------------------


def test_restart_tool_is_marked_is_write_true() -> None:
    """Sanity: the tool MUST be registered with is_write=True so Layer 2 filters it."""
    from k8s_mcp_server.tools._registry import all_tools

    [restart] = [t for t in all_tools() if t.name == "restart_deployment"]
    assert restart.is_write is True


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing name
        {"name": ""},  # empty name
        {"name": "api", "extra": "nope"},  # extra field
    ],
)
def test_restart_input_validation_rejects_bad_payloads(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RestartDeploymentInput.model_validate(payload)
