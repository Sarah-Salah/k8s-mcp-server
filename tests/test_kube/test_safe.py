"""Tests for the namespace allowlist resolver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import NamespaceNotAllowedError, resolve_read_namespaces


def _ctx(default: str = "default") -> KubeContext:
    return KubeContext(
        api_client=MagicMock(),
        context_name="test-context",
        default_namespace=default,
    )


def test_none_returns_context_default_when_no_allowlist() -> None:
    assert resolve_read_namespaces(None, settings=Settings(), ctx=_ctx("default")) == ["default"]


def test_none_uses_overridden_context_default() -> None:
    assert resolve_read_namespaces(None, settings=Settings(), ctx=_ctx("kube-system")) == [
        "kube-system"
    ]


def test_none_with_allowlist_excluding_default_raises() -> None:
    with pytest.raises(NamespaceNotAllowedError) as exc_info:
        resolve_read_namespaces(
            None,
            settings=Settings(namespaces=("dev", "staging")),
            ctx=_ctx("default"),
        )
    msg = str(exc_info.value)
    assert "default" in msg
    assert "specify a namespace explicitly" in msg
    assert "--namespaces" in msg


def test_none_with_allowlist_including_default_returns_default() -> None:
    assert resolve_read_namespaces(
        None,
        settings=Settings(namespaces=("default", "dev")),
        ctx=_ctx("default"),
    ) == ["default"]


def test_all_no_allowlist_returns_none() -> None:
    assert resolve_read_namespaces("all", settings=Settings(), ctx=_ctx()) is None


def test_all_with_allowlist_returns_sorted_allowlist() -> None:
    assert resolve_read_namespaces(
        "all",
        settings=Settings(namespaces=("staging", "dev", "prod")),
        ctx=_ctx(),
    ) == ["dev", "prod", "staging"]


def test_specific_namespace_passes_through_when_allowed() -> None:
    assert resolve_read_namespaces(
        "dev",
        settings=Settings(namespaces=("dev", "staging")),
        ctx=_ctx(),
    ) == ["dev"]


def test_specific_namespace_passes_through_when_no_allowlist() -> None:
    assert resolve_read_namespaces("anything", settings=Settings(), ctx=_ctx()) == ["anything"]


def test_specific_outside_allowlist_raises() -> None:
    with pytest.raises(NamespaceNotAllowedError) as exc_info:
        resolve_read_namespaces(
            "prod",
            settings=Settings(namespaces=("dev", "staging")),
            ctx=_ctx(),
        )
    assert "prod" in str(exc_info.value)
    assert "allowlist" in str(exc_info.value)
