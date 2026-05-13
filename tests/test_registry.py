"""Tests for the tool registry decorator."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from k8s_mcp_server.tools._registry import (
    ToolResult,
    all_tools,
    clear_registry,
    register_tool,
)


class _NoopInput(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot + restore the registry around each test in this module."""
    from k8s_mcp_server.tools import _registry

    snapshot = dict(_registry._REGISTRY)
    clear_registry()
    yield
    _registry._REGISTRY.clear()
    _registry._REGISTRY.update(snapshot)


async def _handler(_inp: _NoopInput) -> ToolResult:
    return ToolResult(success=True, data={})


def test_register_and_list() -> None:
    register_tool(name="tool_a", description="A", input_model=_NoopInput)(_handler)
    tools = all_tools()
    assert [t.name for t in tools] == ["tool_a"]
    assert tools[0].description == "A"
    assert tools[0].is_write is False


def test_duplicate_registration_raises() -> None:
    register_tool(name="dup", description="x", input_model=_NoopInput)(_handler)
    with pytest.raises(RuntimeError, match="already registered"):
        register_tool(name="dup", description="x", input_model=_NoopInput)(_handler)


def test_all_tools_sorted_by_name() -> None:
    register_tool(name="zeta", description="z", input_model=_NoopInput)(_handler)
    register_tool(name="alpha", description="a", input_model=_NoopInput)(_handler)
    register_tool(name="mike", description="m", input_model=_NoopInput)(_handler)
    assert [t.name for t in all_tools()] == ["alpha", "mike", "zeta"]


def test_is_write_flag_recorded() -> None:
    register_tool(name="dangerous", description="d", input_model=_NoopInput, is_write=True)(
        _handler
    )
    [tool] = all_tools()
    assert tool.is_write is True


def test_clear_registry() -> None:
    register_tool(name="ephemeral", description="e", input_model=_NoopInput)(_handler)
    assert all_tools()
    clear_registry()
    assert all_tools() == []


def test_tool_result_envelope_defaults() -> None:
    r = ToolResult(success=True)
    assert r.data is None
    assert r.error is None
    assert r.audit is None
