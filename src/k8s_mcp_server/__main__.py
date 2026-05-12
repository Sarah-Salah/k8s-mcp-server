"""Entry point: ``python -m k8s_mcp_server`` or the ``k8s-mcp-server`` console script."""

from __future__ import annotations

import logging
import sys

from k8s_mcp_server.config import Settings, parse_args

logger = logging.getLogger("k8s_mcp_server")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _run(settings: Settings) -> int:
    logger.info(
        "k8s-mcp-server starting (writes=%s, namespaces=%s, context=%s)",
        settings.enable_writes,
        ",".join(settings.namespaces) if settings.namespaces else "all",
        settings.context or "current",
    )
    # TODO: wire up the MCP stdio server and tool registry.
    logger.warning("server runtime not implemented yet — exiting 0")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    settings = parse_args(argv)
    _setup_logging(settings.log_level)
    return _run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
