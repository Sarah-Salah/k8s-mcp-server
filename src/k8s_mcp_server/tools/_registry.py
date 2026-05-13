"""Tool registry: ``ToolResult`` envelope and the ``@register_tool`` decorator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

__all__ = [
    "RegisteredTool",
    "ToolHandler",
    "ToolResult",
    "all_tools",
    "clear_registry",
    "register_tool",
]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Envelope returned by every tool. Tools never raise into the MCP layer."""

    success: bool
    data: Any = None
    error: str | None = None
    audit: dict[str, Any] | None = None


ToolHandler = Callable[..., Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A tool registered with the server."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    is_write: bool


_REGISTRY: dict[str, RegisteredTool] = {}


def register_tool(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    is_write: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator that records a tool in the global registry.

    The handler must accept the validated input model as its first positional
    argument and any framework dependencies (``ctx``, ``settings``) by keyword.
    """

    def decorator(fn: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise RuntimeError(f"tool already registered: {name}")
        _REGISTRY[name] = RegisteredTool(
            name=name,
            description=description,
            input_model=input_model,
            handler=fn,
            is_write=is_write,
        )
        return fn

    return decorator


def all_tools() -> list[RegisteredTool]:
    """Return every registered tool, sorted by name."""
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def clear_registry() -> None:
    """Test helper: empty the registry."""
    _REGISTRY.clear()
