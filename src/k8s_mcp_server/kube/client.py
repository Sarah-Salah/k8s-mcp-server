"""Kubeconfig loading and Kubernetes API client bootstrap."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

from k8s_mcp_server.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["KubeConfigError", "KubeContext", "load_context"]


class KubeConfigError(RuntimeError):
    """Raised when the kubeconfig cannot be loaded or the context is invalid."""


@dataclass(frozen=True, slots=True)
class KubeContext:
    """Active Kubernetes API client plus the (safe-to-surface) context name."""

    api_client: client.ApiClient
    context_name: str
    default_namespace: str = "default"


def load_context(settings: Settings) -> KubeContext:
    """Load kubeconfig and return an active ``ApiClient`` + context name.

    Honours ``--kubeconfig`` and ``--context``. Never echoes kubeconfig
    contents (see docs/SECURITY.md).

    Raises:
        KubeConfigError: kubeconfig cannot be loaded or context is invalid.
    """
    kubeconfig_path = str(settings.kubeconfig) if settings.kubeconfig else None
    try:
        config.load_kube_config(
            config_file=kubeconfig_path,
            context=settings.context,
        )
    except ConfigException as exc:
        raise KubeConfigError(f"failed to load kubeconfig: {exc}") from exc
    except FileNotFoundError as exc:
        raise KubeConfigError(f"kubeconfig file not found: {exc.filename}") from exc

    context_name, default_namespace = _resolve_context_metadata(kubeconfig_path, settings.context)

    logger.info(
        "kubeconfig loaded (context=%s, default_namespace=%s)",
        context_name,
        default_namespace,
    )
    return KubeContext(
        api_client=client.ApiClient(),
        context_name=context_name,
        default_namespace=default_namespace,
    )


def _resolve_context_metadata(
    kubeconfig_path: str | None, requested_context: str | None
) -> tuple[str, str]:
    """Return (context_name, default_namespace) for the active context.

    Falls back to ``("unknown", "default")`` if the kubeconfig cannot be parsed.
    """
    try:
        contexts, active = config.list_kube_config_contexts(config_file=kubeconfig_path)
    except ConfigException:
        return (requested_context or "unknown", "default")

    target_name = requested_context or (active["name"] if active else None)
    context_name = target_name or "unknown"
    default_namespace = "default"
    for entry in contexts or []:
        if entry["name"] == target_name:
            ns = (entry.get("context") or {}).get("namespace")
            if ns:
                default_namespace = ns
            break
    return (context_name, default_namespace)
