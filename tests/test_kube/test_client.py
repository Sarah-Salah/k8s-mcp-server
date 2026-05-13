"""Tests for ``kube.client.load_context``.

Note: ``client.ApiClient()`` is constructed without mocking because it builds
from in-memory default Configuration with no IO. If a future ``kubernetes``
library version changes that, these tests will fail in a non-obvious way and
this assumption needs revisiting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.config.config_exception import ConfigException

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeConfigError, load_context


@pytest.fixture
def fake_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Patch ``load_kube_config`` and ``list_kube_config_contexts`` on the
    ``config`` module reference held by ``k8s_mcp_server.kube.client``.

    Returns ``(load_mock, list_contexts_mock)`` so tests can configure return
    values / side effects.
    """
    load_mock = MagicMock()
    list_mock = MagicMock()
    monkeypatch.setattr("k8s_mcp_server.kube.client.config.load_kube_config", load_mock)
    monkeypatch.setattr("k8s_mcp_server.kube.client.config.list_kube_config_contexts", list_mock)
    return load_mock, list_mock


def _ctx_entry(name: str, *, namespace: str | None = None) -> dict[str, Any]:
    """Mirror the shape that ``kubernetes.config`` returns for a context entry."""
    inner: dict[str, Any] = {"cluster": "c", "user": "u"}
    if namespace is not None:
        inner["namespace"] = namespace
    return {"name": name, "context": inner}


def test_resolves_context_and_default_namespace(
    fake_kubeconfig: tuple[MagicMock, MagicMock],
) -> None:
    load_mock, list_mock = fake_kubeconfig
    contexts = [_ctx_entry("dev", namespace="dev-ns")]
    list_mock.return_value = (contexts, contexts[0])

    ctx = load_context(Settings())

    assert ctx.context_name == "dev"
    assert ctx.default_namespace == "dev-ns"
    load_mock.assert_called_once_with(config_file=None, context=None)


def test_falls_back_to_default_when_context_has_no_namespace(
    fake_kubeconfig: tuple[MagicMock, MagicMock],
) -> None:
    _, list_mock = fake_kubeconfig
    contexts = [_ctx_entry("dev")]  # no `namespace` key in the inner dict
    list_mock.return_value = (contexts, contexts[0])

    ctx = load_context(Settings())

    assert ctx.context_name == "dev"
    assert ctx.default_namespace == "default"


def test_uses_settings_context_override(
    fake_kubeconfig: tuple[MagicMock, MagicMock],
) -> None:
    load_mock, list_mock = fake_kubeconfig
    contexts = [
        _ctx_entry("dev", namespace="dev-ns"),
        _ctx_entry("prod", namespace="prod-ns"),
    ]
    list_mock.return_value = (contexts, contexts[0])  # active is "dev"

    ctx = load_context(Settings(context="prod"))

    assert ctx.context_name == "prod"
    assert ctx.default_namespace == "prod-ns"
    load_mock.assert_called_once_with(config_file=None, context="prod")


def test_graceful_fallback_when_list_contexts_raises(
    fake_kubeconfig: tuple[MagicMock, MagicMock],
) -> None:
    _, list_mock = fake_kubeconfig
    list_mock.side_effect = ConfigException("no contexts")

    ctx = load_context(Settings())

    assert ctx.context_name == "unknown"
    assert ctx.default_namespace == "default"


def test_graceful_fallback_preserves_settings_context(
    fake_kubeconfig: tuple[MagicMock, MagicMock],
) -> None:
    """When ``--context`` is set but ``list_kube_config_contexts`` fails, the
    requested context name is preserved (it was already validated by
    ``load_kube_config``); the default namespace falls back to ``"default"``.
    User-visible: audit logs reference ``context_name``.
    """
    _, list_mock = fake_kubeconfig
    list_mock.side_effect = ConfigException("kubeconfig parse failure")

    ctx = load_context(Settings(context="prod"))

    assert ctx.context_name == "prod"
    assert ctx.default_namespace == "default"


def test_raises_kube_config_error_on_missing_file(
    fake_kubeconfig: tuple[MagicMock, MagicMock],
) -> None:
    load_mock, _ = fake_kubeconfig
    load_mock.side_effect = FileNotFoundError(
        2, "No such file or directory", "/nonexistent/kubeconfig"
    )

    with pytest.raises(KubeConfigError, match="/nonexistent/kubeconfig"):
        load_context(Settings(kubeconfig=Path("/nonexistent/kubeconfig")))


def test_raises_kube_config_error_on_invalid_content(
    fake_kubeconfig: tuple[MagicMock, MagicMock],
) -> None:
    load_mock, _ = fake_kubeconfig
    load_mock.side_effect = ConfigException("invalid kubeconfig: bad context")

    with pytest.raises(KubeConfigError, match="failed to load kubeconfig"):
        load_context(Settings())
