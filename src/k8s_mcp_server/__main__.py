"""Entry point: ``python -m k8s_mcp_server`` or the ``k8s-mcp-server`` console script."""

from __future__ import annotations

import asyncio
import logging
import sys

from k8s_mcp_server.config import Settings, parse_args
from k8s_mcp_server.kube.client import KubeConfigError
from k8s_mcp_server.server import serve

logger = logging.getLogger("k8s_mcp_server")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _log_startup(settings: Settings) -> None:
    logger.info(
        "k8s-mcp-server starting (writes=%s, namespaces=%s, context=%s)",
        settings.enable_writes,
        ",".join(settings.namespaces) if settings.namespaces else "all",
        settings.context or "current",
    )


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    settings = parse_args(argv)
    _setup_logging(settings.log_level)
    _log_startup(settings)
    try:
        return asyncio.run(serve(settings))
    except KubeConfigError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("interrupted, shutting down")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
