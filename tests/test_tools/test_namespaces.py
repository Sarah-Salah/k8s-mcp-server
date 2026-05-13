"""Tests for the ``list_namespaces`` tool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools.namespaces import ListNamespacesInput, list_namespaces


def _ns(name, *, phase="Active", age_minutes=60):
    """Build a minimal V1Namespace-like object for testing."""
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, creation_timestamp=created),
        status=SimpleNamespace(phase=phase),
    )


@pytest.mark.asyncio
async def test_lists_all_namespaces_when_no_allowlist(
    kube_context: KubeContext, mock_core_v1: MagicMock
) -> None:
    mock_core_v1.list_namespace.return_value = SimpleNamespace(
        items=[_ns("default"), _ns("kube-system"), _ns("dev")]
    )

    result = await list_namespaces(ListNamespacesInput(), ctx=kube_context, settings=Settings())

    assert result.success is True
    names = [n["name"] for n in result.data["namespaces"]]
    assert names == ["default", "dev", "kube-system"]  # sorted by name


@pytest.mark.asyncio
async def test_filters_to_allowlist_when_set(
    kube_context: KubeContext, mock_core_v1: MagicMock
) -> None:
    mock_core_v1.list_namespace.return_value = SimpleNamespace(
        items=[_ns("default"), _ns("kube-system"), _ns("dev"), _ns("staging")]
    )

    result = await list_namespaces(
        ListNamespacesInput(),
        ctx=kube_context,
        settings=Settings(namespaces=("staging", "dev")),  # intentionally unsorted
    )

    assert result.success is True
    names = [n["name"] for n in result.data["namespaces"]]
    assert names == ["dev", "staging"]  # deterministic, sorted regardless of allowlist order


@pytest.mark.asyncio
async def test_returns_empty_list_when_allowlist_excludes_everything(
    kube_context: KubeContext, mock_core_v1: MagicMock
) -> None:
    mock_core_v1.list_namespace.return_value = SimpleNamespace(
        items=[_ns("default"), _ns("kube-system")]
    )

    result = await list_namespaces(
        ListNamespacesInput(),
        ctx=kube_context,
        settings=Settings(namespaces=("nonexistent",)),
    )

    assert result.success is True
    assert result.data == {"namespaces": []}


@pytest.mark.asyncio
async def test_includes_age_fields(kube_context: KubeContext, mock_core_v1: MagicMock) -> None:
    mock_core_v1.list_namespace.return_value = SimpleNamespace(
        items=[_ns("default", age_minutes=180)]
    )

    result = await list_namespaces(ListNamespacesInput(), ctx=kube_context, settings=Settings())

    item = result.data["namespaces"][0]
    assert item["age_seconds"] >= 180 * 60
    assert item["age_seconds"] < 200 * 60  # sanity bound on the test
    assert "h" in item["age_human"]


@pytest.mark.asyncio
async def test_returns_error_on_api_exception(
    kube_context: KubeContext, mock_core_v1: MagicMock
) -> None:
    mock_core_v1.list_namespace.side_effect = ApiException(status=403, reason="Forbidden")

    result = await list_namespaces(ListNamespacesInput(), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert result.data is None
    assert "Forbidden" in (result.error or "")


@pytest.mark.asyncio
async def test_returns_error_on_unexpected_exception(
    kube_context: KubeContext, mock_core_v1: MagicMock
) -> None:
    mock_core_v1.list_namespace.side_effect = RuntimeError("boom")

    result = await list_namespaces(ListNamespacesInput(), ctx=kube_context, settings=Settings())

    assert result.success is False
    assert "boom" in (result.error or "")


def test_input_model_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ListNamespacesInput.model_validate({"foo": "bar"})


@pytest.mark.asyncio
async def test_status_unknown_when_phase_missing(
    kube_context: KubeContext, mock_core_v1: MagicMock
) -> None:
    ns = _ns("default")
    ns.status.phase = None
    mock_core_v1.list_namespace.return_value = SimpleNamespace(items=[ns])

    result = await list_namespaces(ListNamespacesInput(), ctx=kube_context, settings=Settings())

    assert result.data["namespaces"][0]["status"] == "Unknown"
