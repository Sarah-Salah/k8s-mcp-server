"""Safety helpers: namespace allowlist resolution and write-tool gates."""

from __future__ import annotations

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools._registry import ToolResult

__all__ = [
    "NamespaceNotAllowedError",
    "assert_writes_enabled",
    "resolve_read_namespaces",
]


class NamespaceNotAllowedError(ValueError):
    """Requested namespace is not in the ``--namespaces`` allowlist."""


def assert_writes_enabled(settings: Settings) -> ToolResult | None:
    """Layer 3 defense: in-handler re-check of the writes-enabled flag.

    Returns a ``ToolResult`` for the handler to short-circuit on when writes
    are disabled, or ``None`` to indicate "proceed". Per docs/SECURITY.md,
    this is the layer that catches a hypothetical bypass of Layer 2 (the
    registry filter in ``server.build_server``). Every write tool MUST call
    this as the first line of its handler body — see CLAUDE.md §6.1.
    """
    if not settings.enable_writes:
        return ToolResult(
            success=False,
            error=(
                "write operations are disabled; restart the server with --enable-writes to enable"
            ),
        )
    return None


def resolve_read_namespaces(
    requested: str | None,
    *,
    settings: Settings,
    ctx: KubeContext,
) -> list[str] | None:
    """Resolve a tool's ``namespace`` argument into the actual namespaces to query.

    Returns:
        - A ``list[str]`` of namespace names to iterate, or
        - ``None`` to mean "every namespace in the cluster". Only returned when
          no allowlist is configured and the caller asked for ``"all"``.

    Raises:
        NamespaceNotAllowedError: the requested namespace falls outside the allowlist,
            or ``requested is None`` and the context's default namespace is not
            allowlisted.
    """
    allow = set(settings.namespaces) if settings.namespaces else None

    if requested is None:
        ns = ctx.default_namespace
        if allow is not None and ns not in allow:
            raise NamespaceNotAllowedError(
                f"context default namespace '{ns}' is not in the configured allowlist; "
                f"specify a namespace explicitly or restart with --namespaces including '{ns}'"
            )
        return [ns]

    if requested == "all":
        return sorted(allow) if allow is not None else None

    if allow is not None and requested not in allow:
        raise NamespaceNotAllowedError(
            f"namespace '{requested}' is not in the configured allowlist"
        )
    return [requested]
