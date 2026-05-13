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
from k8s_mcp_server.tools.deployments import ListDeploymentsInput, list_deployments

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
