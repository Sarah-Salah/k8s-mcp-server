"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from k8s_mcp_server.kube.client import KubeContext


@pytest.fixture
def kube_context() -> KubeContext:
    """A ``KubeContext`` with a MagicMock api_client and a fixed context name.

    Tests that need to control K8s API responses should use the ``mock_core_v1``
    fixture (or monkeypatch the relevant API class in their tool module).
    """
    return KubeContext(api_client=MagicMock(), context_name="test-context")


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
