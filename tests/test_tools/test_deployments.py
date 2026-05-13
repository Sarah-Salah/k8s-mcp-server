"""Tests for the ``list_deployments`` tool."""

from __future__ import annotations

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
from k8s_mcp_server.tools.deployments import (
    GetDeploymentInput,
    ListDeploymentsInput,
    get_deployment,
    list_deployments,
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
