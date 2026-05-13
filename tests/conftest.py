"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from unittest.mock import MagicMock

import pytest

from k8s_mcp_server.kube.client import KubeContext


@pytest.fixture
def kube_context() -> KubeContext:
    """A ``KubeContext`` with a MagicMock api_client and a fixed context name."""
    return KubeContext(
        api_client=MagicMock(),
        context_name="test-context",
        default_namespace="default",
    )


@pytest.fixture
def mock_core_v1(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Replace ``CoreV1Api`` inside ``tools.namespaces`` with a MagicMock instance.

    Yields the mock so tests can configure return values / side effects on
    ``mock_core_v1.list_namespace``, etc.
    """
    api = MagicMock()
    monkeypatch.setattr(
        "k8s_mcp_server.tools.namespaces.CoreV1Api",
        lambda _client: api,
    )
    yield api


@pytest.fixture
def patch_core_v1(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], MagicMock]:
    """Factory: patch ``CoreV1Api`` inside an arbitrary tool module.

    Usage:
        api = patch_core_v1("k8s_mcp_server.tools.pods")
        api.list_namespaced_pod.return_value = ...
    """

    def _patch(target_module: str) -> MagicMock:
        api = MagicMock()
        monkeypatch.setattr(f"{target_module}.CoreV1Api", lambda _client: api)
        return api

    return _patch


@pytest.fixture
def patch_apps_v1(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], MagicMock]:
    """Factory: patch ``AppsV1Api`` inside an arbitrary tool module.

    Mirrors :func:`patch_core_v1` for tools that use the apps/v1 API
    (deployments, future StatefulSets / DaemonSets in v2). Kept as a
    parallel fixture rather than generalising the existing one to avoid
    a refactor inside a feature commit.

    Usage:
        api = patch_apps_v1("k8s_mcp_server.tools.deployments")
        api.list_namespaced_deployment.return_value = ...
    """

    def _patch(target_module: str) -> MagicMock:
        api = MagicMock()
        monkeypatch.setattr(f"{target_module}.AppsV1Api", lambda _client: api)
        return api

    return _patch


@pytest.fixture
def patch_networking_v1(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], MagicMock]:
    """Factory: patch ``NetworkingV1Api`` inside an arbitrary tool module.

    Mirrors :func:`patch_core_v1` and :func:`patch_apps_v1` for tools that
    use the networking.k8s.io/v1 API (Ingress).

    Usage:
        api = patch_networking_v1("k8s_mcp_server.tools.describe")
        api.read_namespaced_ingress.return_value = ...
    """

    def _patch(target_module: str) -> MagicMock:
        api = MagicMock()
        monkeypatch.setattr(f"{target_module}.NetworkingV1Api", lambda _client: api)
        return api

    return _patch
