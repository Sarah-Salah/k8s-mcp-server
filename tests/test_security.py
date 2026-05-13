"""Cross-cutting security tests for the write-tool contract.

Pins the three layers of defense from ``docs/SECURITY.md``:
    Layer 1 — ``--enable-writes`` flag at server start (Settings.enable_writes)
    Layer 2 — registry filter in ``server.build_server`` excludes write tools
    Layer 3 — ``assert_writes_enabled`` in-handler re-check

Plus the audit logging contract from ``utils.audit.log_write_operation``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.kube.safe import assert_writes_enabled
from k8s_mcp_server.server import _visible_tools
from k8s_mcp_server.tools._registry import (
    _REGISTRY,
    ToolResult,
    clear_registry,
    register_tool,
)
from k8s_mcp_server.utils.audit import log_write_operation

_DUMMY_WRITE_NAME = "_dummy_write_for_test"
_DUMMY_READ_NAME = "_dummy_read_for_test"


class _DummyInput(BaseModel):
    """Shared by both dummy tools — both take no inputs."""

    model_config = ConfigDict(extra="forbid")


async def _dummy_handler(_inp: _DummyInput) -> ToolResult:
    return ToolResult(success=True)


@pytest.fixture(autouse=True)
def _isolated_registry_with_dummy_tools() -> Iterator[None]:
    """Snapshot + clear the real registry, install only dummy read+write tools,
    then restore on teardown.

    Lets every test in this file reason about the registry in isolation
    without the 13 real read tools (or any future write tools) interfering.
    """
    snapshot = dict(_REGISTRY)
    clear_registry()
    register_tool(
        name=_DUMMY_WRITE_NAME,
        description="test-only dummy write tool",
        input_model=_DummyInput,
        is_write=True,
    )(_dummy_handler)
    register_tool(
        name=_DUMMY_READ_NAME,
        description="test-only dummy read tool",
        input_model=_DummyInput,
        is_write=False,
    )(_dummy_handler)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# Layer 2 — registry filter at server bootstrap
# ---------------------------------------------------------------------------


def test_write_tools_excluded_when_writes_disabled() -> None:
    visible = _visible_tools(Settings(enable_writes=False))
    names = {t.name for t in visible}
    assert _DUMMY_WRITE_NAME not in names
    assert _DUMMY_READ_NAME in names


def test_write_tools_included_when_writes_enabled() -> None:
    visible = _visible_tools(Settings(enable_writes=True))
    names = {t.name for t in visible}
    assert _DUMMY_WRITE_NAME in names
    assert _DUMMY_READ_NAME in names


def test_read_tools_always_visible_regardless_of_flag() -> None:
    """Sanity: the flag must NOT accidentally filter read tools."""
    assert any(t.name == _DUMMY_READ_NAME for t in _visible_tools(Settings()))
    assert any(t.name == _DUMMY_READ_NAME for t in _visible_tools(Settings(enable_writes=True)))


def test_visible_tools_is_default_subset_of_all_tools() -> None:
    """With writes off (default), visible ⊆ registry, missing exactly the write tools."""
    from k8s_mcp_server.tools._registry import all_tools

    visible_names = {t.name for t in _visible_tools(Settings())}
    all_names = {t.name for t in all_tools()}
    write_names = {t.name for t in all_tools() if t.is_write}
    assert visible_names == all_names - write_names


# ---------------------------------------------------------------------------
# Layer 3 — assert_writes_enabled
# ---------------------------------------------------------------------------


def test_assert_writes_enabled_returns_friendly_error_when_disabled() -> None:
    result = assert_writes_enabled(Settings(enable_writes=False))

    assert result is not None
    assert result.success is False
    assert result.error == (
        "write operations are disabled; restart the server with --enable-writes to enable"
    )


def test_assert_writes_enabled_returns_none_when_enabled() -> None:
    assert assert_writes_enabled(Settings(enable_writes=True)) is None


def test_assert_writes_enabled_returns_tool_result_envelope_when_denied() -> None:
    """The denied path returns a fully-shaped ToolResult so handlers can return
    it as-is to the MCP layer."""
    result = assert_writes_enabled(Settings())  # default: writes disabled

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.data is None
    assert result.audit is None
    assert result.error is not None and result.error.startswith("write operations are disabled")


# ---------------------------------------------------------------------------
# Audit logging — log_write_operation
# ---------------------------------------------------------------------------


def test_log_write_operation_emits_at_info_level_with_stable_prefix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        log_write_operation("scale_deployment", namespace="dev", name="api", dry_run=True)

    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert record.message.startswith("write_operation ")
    assert "tool=scale_deployment" in record.message


def test_log_write_operation_includes_all_kwargs_as_keyvals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        log_write_operation(
            "scale_deployment",
            namespace="staging",
            name="api",
            replicas_from=3,
            replicas_to=5,
            dry_run=False,
        )

    assert (
        caplog.records[0].message
        == "write_operation tool=scale_deployment namespace=staging name=api "
        "replicas_from=3 replicas_to=5 dry_run=False"
    )


def test_log_write_operation_with_no_kwargs_emits_clean_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Edge case: no fields → no trailing whitespace, just `tool=<name>`."""
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        log_write_operation("some_tool")

    assert caplog.records[0].message == "write_operation tool=some_tool"


def test_log_write_operation_logger_name_is_audit_namespace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The logger name is part of the public contract — operators configure
    log aggregation on ``k8s_mcp_server.audit``. Do not rename without a
    major version bump.
    """
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        log_write_operation("foo")

    assert caplog.records[0].name == "k8s_mcp_server.audit"


def test_log_write_operation_redacts_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The most common accidental leak — a password value passed through audit."""
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        log_write_operation("dangerous_tool", password="hunter2", safe_field="ok")

    msg = caplog.records[0].message
    assert "hunter2" not in msg
    assert "password=<redacted>" in msg
    # Non-sensitive fields pass through untouched
    assert "safe_field=ok" in msg


@pytest.mark.parametrize(
    "field,value",
    [
        ("token", "abc123"),
        ("secret", "foo-bar-baz"),
        ("password", "hunter2"),
        ("apikey", "xyz789"),
        ("api_key", "xyz789"),
        ("api-key", "xyz789"),
        ("bearer", "eyJabcdef"),
        # Case-insensitive
        ("TOKEN", "UPPERCASE"),
        ("Password", "MixedCase"),
    ],
)
def test_log_write_operation_redacts_sensitive_field_names(
    field: str,
    value: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every pattern from the SECURITY.md regex is exercised, including
    case-insensitive variants and the ``api[_-]?key`` punctuation variants.
    """
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        log_write_operation("tool", **{field: value})

    msg = caplog.records[0].message
    assert value not in msg
    assert f"{field}=<redacted>" in msg


def test_log_write_operation_does_not_redact_unrelated_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive: the redaction is field-name-based, not entropy-based.
    A value that looks secret-like but lives under a non-matching field
    name passes through.
    """
    with caplog.at_level(logging.INFO, logger="k8s_mcp_server.audit"):
        log_write_operation(
            "tool", request_id="abc-123-xyz", uid="00000000-0000-0000-0000-000000000000"
        )

    msg = caplog.records[0].message
    assert "abc-123-xyz" in msg
    assert "00000000-0000-0000-0000-000000000000" in msg


# ---------------------------------------------------------------------------
# Integration — dummy write tool can be invoked when writes enabled
# ---------------------------------------------------------------------------


def test_dummy_write_tool_is_callable_when_writes_enabled() -> None:
    """End-to-end smoke: when the flag is on, the dummy write tool shows up
    in the visible list and its handler is the one we registered.
    """
    visible = _visible_tools(Settings(enable_writes=True))
    [dummy] = [t for t in visible if t.name == _DUMMY_WRITE_NAME]
    assert dummy.is_write is True
    assert dummy.input_model is _DummyInput


def test_dummy_kube_context_unused_by_assert_writes_enabled() -> None:
    """``assert_writes_enabled`` reads only ``settings`` — no KubeContext
    dependency. Confirming the function signature stays simple.
    """
    # If this test breaks because the signature now requires ctx, that's a
    # behaviour change to flag.
    ctx = KubeContext(api_client=MagicMock(), context_name="test", default_namespace="default")
    del ctx  # not used by assert_writes_enabled
    assert assert_writes_enabled(Settings(enable_writes=True)) is None
