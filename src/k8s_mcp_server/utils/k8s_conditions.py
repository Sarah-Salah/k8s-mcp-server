"""Helpers for working with Kubernetes condition objects.

``V1PodCondition``, ``V1DeploymentCondition``, and ``V1NodeCondition`` all
share the same shape (``type``, ``status``, ``reason``, ``message``,
``last_transition_time``), so the same formatter applies to all three.
"""

from __future__ import annotations

from typing import Any

from k8s_mcp_server.utils.formatting import age_seconds_since

__all__ = ["format_condition"]


def format_condition(cond: Any) -> dict[str, Any]:
    """Trim a K8s condition object into the LLM-friendly dict used across tools.

    Returns ``{type, status, reason, message, last_transition_age_seconds}``.
    The age field is ``None`` when ``last_transition_time`` is missing.
    """
    transition = getattr(cond, "last_transition_time", None)
    return {
        "type": getattr(cond, "type", None),
        "status": getattr(cond, "status", None),
        "reason": getattr(cond, "reason", None),
        "message": getattr(cond, "message", None),
        "last_transition_age_seconds": (
            age_seconds_since(transition) if transition is not None else None
        ),
    }
