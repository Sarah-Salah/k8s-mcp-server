"""Tests for the example MCP config files in ``examples/``.

A typo in these JSON files breaks Claude Desktop silently — the GUI logs the
parse error to a place most users don't check, then the server just doesn't
appear. These tests prevent that user-facing bug class.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_EXAMPLES_DIR = Path(__file__).parent.parent / "examples"
_READ_ONLY = _EXAMPLES_DIR / "claude_desktop_config.read_only.json"
_WITH_WRITES = _EXAMPLES_DIR / "claude_desktop_config.with_writes.json"


@pytest.mark.parametrize("path", [_READ_ONLY, _WITH_WRITES])
def test_example_config_parses_as_json(path: Path) -> None:
    """A typo here = silent Claude Desktop failure for every user who pasted it."""
    json.loads(path.read_text())


@pytest.mark.parametrize("path", [_READ_ONLY, _WITH_WRITES])
def test_example_config_has_required_mcp_server_structure(path: Path) -> None:
    """``mcpServers.kubernetes`` must have ``command`` and ``args`` keys with the right types."""
    config = json.loads(path.read_text())

    assert "mcpServers" in config
    assert "kubernetes" in config["mcpServers"]
    server = config["mcpServers"]["kubernetes"]
    assert server["command"] == "k8s-mcp-server"
    assert isinstance(server["args"], list)
    assert all(isinstance(a, str) for a in server["args"])


def test_with_writes_example_includes_enable_writes_and_namespaces() -> None:
    """The with-writes example must opt into both safety levers — never just one."""
    config = json.loads(_WITH_WRITES.read_text())
    args = config["mcpServers"]["kubernetes"]["args"]

    assert "--enable-writes" in args
    assert "--namespaces" in args
    # The --namespaces flag must be followed by a value (not be the last arg).
    namespaces_idx = args.index("--namespaces")
    assert namespaces_idx + 1 < len(args), "--namespaces must have a value following it"
