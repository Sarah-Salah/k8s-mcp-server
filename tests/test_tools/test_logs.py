"""Tests for the ``get_pod_logs`` tool."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools.logs import GetPodLogsInput, get_pod_logs

PATCH_TARGET = "k8s_mcp_server.tools.logs"


def _pod_with_containers(*names: str) -> SimpleNamespace:
    return SimpleNamespace(
        spec=SimpleNamespace(containers=[SimpleNamespace(name=n) for n in names]),
    )


@pytest.fixture
def logs_api(patch_core_v1: Callable[[str], MagicMock]) -> MagicMock:
    return patch_core_v1(PATCH_TARGET)


# ---------------------------------------------------------------------------
# Happy paths and namespace handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_logs_string_and_container(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod_log.return_value = "line 1\nline 2\nline 3\n"

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data == {
        "logs": "line 1\nline 2\nline 3\n",
        "truncated": False,
        "container": "app",
    }
    logs_api.read_namespaced_pod.assert_not_called()  # container provided → no preflight


@pytest.mark.asyncio
async def test_uses_default_namespace_when_none(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod.return_value = _pod_with_containers("app")
    logs_api.read_namespaced_pod_log.return_value = ""

    await get_pod_logs(GetPodLogsInput(name="api"), ctx=kube_context, settings=Settings())

    assert logs_api.read_namespaced_pod_log.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_specific_namespace_passes_through(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod_log.return_value = ""

    await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert logs_api.read_namespaced_pod_log.call_args.kwargs["namespace"] == "dev"


@pytest.mark.asyncio
async def test_rejects_namespace_all(kube_context: KubeContext, logs_api: MagicMock) -> None:
    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="all", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "single namespace" in (result.error or "")
    logs_api.read_namespaced_pod_log.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="prod", container="app"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")


@pytest.mark.asyncio
async def test_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    result = await get_pod_logs(
        GetPodLogsInput(name="api", container="app"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


# ---------------------------------------------------------------------------
# Container resolution (pre-flight)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_picks_single_container_when_not_specified(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod.return_value = _pod_with_containers("only-one")
    logs_api.read_namespaced_pod_log.return_value = "log\n"

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    assert result.data["container"] == "only-one"
    assert logs_api.read_namespaced_pod_log.call_args.kwargs["container"] == "only-one"


@pytest.mark.asyncio
async def test_multi_container_without_container_returns_error_with_list(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod.return_value = _pod_with_containers(
        "app", "sidecar", "log-shipper"
    )

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    err = result.error or ""
    assert "multiple containers" in err
    assert "app" in err
    assert "sidecar" in err
    assert "log-shipper" in err
    assert "'container' parameter" in err
    logs_api.read_namespaced_pod_log.assert_not_called()


@pytest.mark.asyncio
async def test_pod_with_no_containers_defined_returns_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod.return_value = SimpleNamespace(spec=SimpleNamespace(containers=[]))

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "no containers defined" in (result.error or "")


@pytest.mark.asyncio
async def test_pod_with_missing_spec_returns_no_containers_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod.return_value = SimpleNamespace(spec=None)

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "no containers defined" in (result.error or "")


@pytest.mark.asyncio
async def test_pod_not_found_during_preflight_friendly_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

    result = await get_pod_logs(
        GetPodLogsInput(name="ghost", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "ghost" in (result.error or "")
    assert "dev" in (result.error or "")
    assert "not found" in (result.error or "")
    logs_api.read_namespaced_pod_log.assert_not_called()


@pytest.mark.asyncio
async def test_non_404_preflight_error_returns_kubernetes_api_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod.side_effect = ApiException(status=500, reason="Internal")

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")


@pytest.mark.asyncio
async def test_resolve_container_returns_empty_string_when_name_missing(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    """Defensive: container with no .name still returns a string (empty), not None."""
    logs_api.read_namespaced_pod.return_value = SimpleNamespace(
        spec=SimpleNamespace(containers=[SimpleNamespace()])  # no name attribute
    )
    logs_api.read_namespaced_pod_log.return_value = ""

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    assert result.data["container"] == ""


# ---------------------------------------------------------------------------
# K8s param forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_tail_lines_since_seconds_previous_to_api(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod_log.return_value = ""

    await get_pod_logs(
        GetPodLogsInput(
            name="api",
            namespace="dev",
            container="app",
            tail_lines=50,
            since_seconds=3600,
            previous=True,
        ),
        ctx=kube_context,
        settings=Settings(),
    )

    kwargs = logs_api.read_namespaced_pod_log.call_args.kwargs
    assert kwargs["tail_lines"] == 50
    assert kwargs["since_seconds"] == 3600
    assert kwargs["previous"] is True
    assert kwargs["container"] == "app"


# ---------------------------------------------------------------------------
# Log API errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pod_not_found_from_log_call_friendly_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    """Container is provided so no preflight; the 404 comes from the log call."""
    logs_api.read_namespaced_pod_log.side_effect = ApiException(status=404, reason="Not Found")

    result = await get_pod_logs(
        GetPodLogsInput(name="ghost", namespace="dev", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "ghost" in (result.error or "")
    assert "dev" in (result.error or "")
    assert "not found" in (result.error or "")
    logs_api.read_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_previous_with_no_prior_instance_friendly_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod_log.side_effect = ApiException(status=400, reason="Bad Request")

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app", previous=True),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    err = result.error or ""
    assert "no previous logs" in err
    assert "api" in err
    assert "app" in err
    assert "not been restarted" in err


@pytest.mark.asyncio
async def test_previous_400_without_container_arg_omits_container_phrase(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    """Defensive: friendly previous-error formatting works even if container resolved
    to an empty string."""
    logs_api.read_namespaced_pod.return_value = SimpleNamespace(
        spec=SimpleNamespace(containers=[SimpleNamespace(name="")])
    )
    logs_api.read_namespaced_pod_log.side_effect = ApiException(status=400, reason="Bad Request")

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", previous=True),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    err = result.error or ""
    assert "no previous logs" in err
    # No "container ''" phrase: empty container name should suppress the parenthetical
    assert "container ''" not in err


@pytest.mark.asyncio
async def test_non_404_non_previous_400_returns_kubernetes_api_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod_log.side_effect = ApiException(status=500, reason="Internal")

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")


@pytest.mark.asyncio
async def test_unexpected_exception_returns_error(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    logs_api.read_namespaced_pod_log.side_effect = RuntimeError("boom")

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncates_when_exceeds_max_bytes(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    # 5000 lines of 100 bytes each = 500 KB; cap at 100 KB.
    big = "".join(f"line {i:04d} " + "x" * 90 + "\n" for i in range(5000))
    assert len(big.encode("utf-8")) > 200_000  # sanity: well over the cap
    logs_api.read_namespaced_pod_log.return_value = big

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app", max_bytes=100_000),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["truncated"] is True
    assert len(result.data["logs"].encode("utf-8")) <= 100_000
    # Most-recent kept: last line of the original input should still be present
    assert "line 4999" in result.data["logs"]
    # First kept line is whole (no half-line at start)
    assert not result.data["logs"].startswith("ine ")  # would indicate mid-line cut


@pytest.mark.asyncio
async def test_does_not_truncate_when_under_max_bytes(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    small = "tiny log\n"
    logs_api.read_namespaced_pod_log.return_value = small

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app", max_bytes=100_000),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is False
    assert result.data["logs"] == small


@pytest.mark.asyncio
async def test_empty_logs_does_not_truncate(kube_context: KubeContext, logs_api: MagicMock) -> None:
    logs_api.read_namespaced_pod_log.return_value = ""

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data == {"logs": "", "truncated": False, "container": "app"}


@pytest.mark.asyncio
async def test_truncation_with_no_newlines_returns_truncated_text(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    """Defensive: a log payload with no newlines should still truncate."""
    blob = "x" * 5000
    logs_api.read_namespaced_pod_log.return_value = blob

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app", max_bytes=1024),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["truncated"] is True
    assert len(result.data["logs"].encode("utf-8")) <= 1024


@pytest.mark.asyncio
async def test_logs_none_from_api_returns_empty_string(
    kube_context: KubeContext, logs_api: MagicMock
) -> None:
    """Defensive: K8s client occasionally returns None for empty logs."""
    logs_api.read_namespaced_pod_log.return_value = None

    result = await get_pod_logs(
        GetPodLogsInput(name="api", namespace="dev", container="app"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["logs"] == ""
    assert result.data["truncated"] is False


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing name
        {"name": ""},  # empty name
        {"name": "x", "extra": "nope"},  # extra field
        {"name": "x", "tail_lines": 0},
        {"name": "x", "tail_lines": 10001},
        {"name": "x", "since_seconds": 0},
        {"name": "x", "max_bytes": 512},  # below MIN_MAX_BYTES (1024)
        {"name": "x", "max_bytes": 2_000_000},  # above MAX_MAX_BYTES (1 MiB)
    ],
)
def test_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GetPodLogsInput.model_validate(payload)
