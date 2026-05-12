"""CLI argument parsing and resolved server settings."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from k8s_mcp_server import __version__

__all__ = ["LOG_LEVELS", "Settings", "build_parser", "parse_args"]

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved server settings, parsed from CLI args."""

    enable_writes: bool = False
    namespaces: tuple[str, ...] | None = None
    kubeconfig: Path | None = None
    context: str | None = None
    log_level: str = "INFO"


def _parse_namespaces(value: str) -> tuple[str, ...]:
    items = tuple(s.strip() for s in value.split(",") if s.strip())
    if not items:
        raise argparse.ArgumentTypeError("--namespaces requires at least one value")
    return items


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="k8s-mcp-server",
        description="MCP server exposing Kubernetes cluster operations to AI assistants.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"k8s-mcp-server {__version__}",
    )
    parser.add_argument(
        "--enable-writes",
        action="store_true",
        help="Register write tools (scale, restart, delete). Off by default.",
    )
    parser.add_argument(
        "--namespaces",
        type=_parse_namespaces,
        default=None,
        metavar="NS1,NS2",
        help="Restrict every tool to these namespaces.",
    )
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to kubeconfig (defaults to ~/.kube/config or $KUBECONFIG).",
    )
    parser.add_argument(
        "--context",
        type=str,
        default=None,
        metavar="NAME",
        help="kubeconfig context to use (defaults to current-context).",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default="INFO",
        help="Logging verbosity (default: INFO).",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> Settings:
    """Parse argv into a Settings object."""
    ns = build_parser().parse_args(argv)
    return Settings(
        enable_writes=ns.enable_writes,
        namespaces=ns.namespaces,
        kubeconfig=ns.kubeconfig,
        context=ns.context,
        log_level=ns.log_level,
    )
