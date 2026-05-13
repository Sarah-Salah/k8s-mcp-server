"""MCP server bootstrap and tool dispatcher."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import ValidationError

from k8s_mcp_server import __version__
from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext, load_context
from k8s_mcp_server.tools import logs as _logs  # noqa: F401 — registers tool
from k8s_mcp_server.tools import namespaces as _namespaces  # noqa: F401 — registers tool
from k8s_mcp_server.tools import pods as _pods  # noqa: F401 — registers tool
from k8s_mcp_server.tools._registry import RegisteredTool, ToolResult, all_tools

logger = logging.getLogger(__name__)

__all__ = ["build_server", "serve"]


def _result_to_text(result: ToolResult) -> list[TextContent]:
    payload: dict[str, Any] = {
        "success": result.success,
        "data": result.data,
        "error": result.error,
        "audit": result.audit,
    }
    return [TextContent(type="text", text=json.dumps(payload, default=str))]


def _visible_tools(settings: Settings) -> list[RegisteredTool]:
    return [t for t in all_tools() if settings.enable_writes or not t.is_write]


def build_server(settings: Settings, ctx: KubeContext) -> Server:
    """Construct the MCP ``Server`` and bind list/call handlers.

    Write tools are filtered out of registration entirely when
    ``settings.enable_writes`` is False (Layer 2 of SECURITY.md).
    """
    server: Server = Server("k8s-mcp-server", version=__version__)
    visible = _visible_tools(settings)
    by_name: dict[str, RegisteredTool] = {t.name: t for t in visible}

    logger.info(
        "registered %d tool(s): %s",
        len(visible),
        ", ".join(t.name for t in visible) or "(none)",
    )

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def _handle_list_tools() -> list[Tool]:
        return [
            Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.input_model.model_json_schema(),
            )
            for t in visible
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        tool = by_name.get(name)
        if tool is None:
            return _result_to_text(ToolResult(success=False, error=f"unknown tool: {name}"))
        try:
            inp = tool.input_model.model_validate(arguments or {})
        except ValidationError as exc:
            return _result_to_text(ToolResult(success=False, error=f"invalid input: {exc}"))
        result = await tool.handler(inp, ctx=ctx, settings=settings)
        return _result_to_text(result)

    return server


async def serve(settings: Settings) -> int:
    """Run the MCP stdio server until the client disconnects."""
    ctx = load_context(settings)
    server = build_server(settings, ctx)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
    return 0
