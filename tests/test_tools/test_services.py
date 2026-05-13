"""Tests for the ``list_services`` tool."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import KubeContext
from k8s_mcp_server.tools.services import ListServicesInput, list_services

PATCH_TARGET = "k8s_mcp_server.tools.services"


def _port(
    *,
    name: str | None = None,
    port: int = 80,
    target_port: int | str = 8080,
    protocol: str | None = "TCP",
    node_port: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        port=port,
        target_port=target_port,
        protocol=protocol,
        node_port=node_port,
    )


def _service(
    name: str,
    *,
    namespace: str = "default",
    type_: str = "ClusterIP",
    cluster_ip: str | None = "10.96.0.1",
    ingress: list[SimpleNamespace] | None = None,
    ports: list[SimpleNamespace] | None = None,
    age_minutes: int = 60,
) -> SimpleNamespace:
    created = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace, creation_timestamp=created),
        spec=SimpleNamespace(
            type=type_,
            cluster_ip=cluster_ip,
            ports=ports if ports is not None else [_port()],
        ),
        status=SimpleNamespace(load_balancer=SimpleNamespace(ingress=ingress)),
    )


@pytest.fixture
def services_api(patch_core_v1: Callable[[str], MagicMock]) -> MagicMock:
    return patch_core_v1(PATCH_TARGET)


# ---------------------------------------------------------------------------
# Namespace dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specific_namespace_calls_list_namespaced_service(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", namespace="dev")]
    )

    result = await list_services(
        ListServicesInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    services_api.list_namespaced_service.assert_called_once()
    assert services_api.list_namespaced_service.call_args.kwargs["namespace"] == "dev"
    services_api.list_service_for_all_namespaces.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_none_uses_context_default(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(items=[])

    await list_services(ListServicesInput(), ctx=kube_context, settings=Settings())

    assert services_api.list_namespaced_service.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_all_no_allowlist_calls_list_service_for_all_namespaces(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_service_for_all_namespaces.return_value = SimpleNamespace(
        items=[_service("api"), _service("web", namespace="prod")]
    )

    result = await list_services(
        ListServicesInput(namespace="all"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    services_api.list_service_for_all_namespaces.assert_called_once()
    services_api.list_namespaced_service.assert_not_called()


@pytest.mark.asyncio
async def test_all_with_allowlist_iterates_allowlisted_namespaces(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.side_effect = [
        SimpleNamespace(items=[_service("dev-svc", namespace="dev")]),
        SimpleNamespace(items=[_service("staging-svc", namespace="staging")]),
    ]

    result = await list_services(
        ListServicesInput(namespace="all"),
        ctx=kube_context,
        settings=Settings(namespaces=("staging", "dev")),
    )

    assert result.success is True
    services_api.list_service_for_all_namespaces.assert_not_called()
    called_namespaces = [
        call.kwargs["namespace"] for call in services_api.list_namespaced_service.call_args_list
    ]
    assert called_namespaces == ["dev", "staging"]
    names = [s["name"] for s in result.data["services"]]
    assert names == ["dev-svc", "staging-svc"]


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    result = await list_services(
        ListServicesInput(namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")


@pytest.mark.asyncio
async def test_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    result = await list_services(
        ListServicesInput(),
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


# ---------------------------------------------------------------------------
# Selector / truncation / sort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_selector_passed_to_api(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(items=[])

    await list_services(
        ListServicesInput(namespace="dev", label_selector="app=api,tier=backend"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert (
        services_api.list_namespaced_service.call_args.kwargs["label_selector"]
        == "app=api,tier=backend"
    )


@pytest.mark.asyncio
async def test_truncated_true_when_results_exceed_limit(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_service_for_all_namespaces.return_value = SimpleNamespace(
        items=[_service(f"s{i}") for i in range(8)]
    )

    result = await list_services(
        ListServicesInput(namespace="all", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is True
    assert len(result.data["services"]) == 5


@pytest.mark.asyncio
async def test_truncated_false_when_results_fit_in_limit(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_service_for_all_namespaces.return_value = SimpleNamespace(
        items=[_service("only")]
    )

    result = await list_services(
        ListServicesInput(namespace="all", limit=5),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["truncated"] is False
    assert len(result.data["services"]) == 1


@pytest.mark.asyncio
async def test_services_sorted_by_namespace_then_name(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_service_for_all_namespaces.return_value = SimpleNamespace(
        items=[
            _service("zeta", namespace="dev"),
            _service("alpha", namespace="prod"),
            _service("beta", namespace="dev"),
        ]
    )

    result = await list_services(
        ListServicesInput(namespace="all"), ctx=kube_context, settings=Settings()
    )

    sequence = [(s["namespace"], s["name"]) for s in result.data["services"]]
    assert sequence == [("dev", "beta"), ("dev", "zeta"), ("prod", "alpha")]


# ---------------------------------------------------------------------------
# external_ip resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clusterip_service_external_ip_is_none(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", type_="ClusterIP", ingress=None)]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    [out] = result.data["services"]
    assert out["type"] == "ClusterIP"
    assert out["external_ip"] is None


@pytest.mark.asyncio
async def test_loadbalancer_service_external_ip_from_ingress_ip(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[
            _service(
                "api",
                type_="LoadBalancer",
                ingress=[SimpleNamespace(ip="203.0.113.5", hostname=None)],
            )
        ]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["services"][0]["external_ip"] == "203.0.113.5"


@pytest.mark.asyncio
async def test_loadbalancer_service_falls_back_to_ingress_hostname(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[
            _service(
                "api",
                type_="LoadBalancer",
                ingress=[SimpleNamespace(ip=None, hostname="abc-123.us-east-1.elb.amazonaws.com")],
            )
        ]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["services"][0]["external_ip"] == "abc-123.us-east-1.elb.amazonaws.com"


@pytest.mark.asyncio
async def test_loadbalancer_with_no_ingress_external_ip_is_none(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    """LoadBalancer still being provisioned has an empty ingress list."""
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", type_="LoadBalancer", ingress=[])]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["services"][0]["external_ip"] is None


@pytest.mark.asyncio
async def test_loadbalancer_with_empty_ingress_entry_external_ip_is_none(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    """Defensive: ingress entry with both ip=None and hostname=None → None."""
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[
            _service(
                "api",
                type_="LoadBalancer",
                ingress=[SimpleNamespace(ip=None, hostname=None)],
            )
        ]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["services"][0]["external_ip"] is None


# ---------------------------------------------------------------------------
# Port formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ports_protocol_defaults_to_tcp_when_missing(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", ports=[_port(protocol=None)])]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["services"][0]["ports"][0]["protocol"] == "TCP"


@pytest.mark.asyncio
async def test_ports_omit_node_port_when_none(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", ports=[_port(node_port=None)])]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert "node_port" not in result.data["services"][0]["ports"][0]


@pytest.mark.asyncio
async def test_ports_include_node_port_when_set(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", ports=[_port(node_port=30080)])]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["services"][0]["ports"][0]["node_port"] == 30080


@pytest.mark.asyncio
async def test_target_port_can_be_string_named_port(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", ports=[_port(name="http", target_port="http")])]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    port = result.data["services"][0]["ports"][0]
    assert port["name"] == "http"
    assert port["target_port"] == "http"


@pytest.mark.asyncio
async def test_multiple_ports_preserved_in_order(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[
            _service(
                "api",
                type_="LoadBalancer",
                ports=[
                    _port(name="http", port=80, target_port="http", node_port=30080),
                    _port(name="https", port=443, target_port=8443, node_port=30443),
                ],
            )
        ]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    ports = result.data["services"][0]["ports"]
    assert len(ports) == 2
    assert ports[0]["name"] == "http"
    assert ports[1]["name"] == "https"
    assert ports[0]["node_port"] == 30080
    assert ports[1]["node_port"] == 30443


@pytest.mark.asyncio
async def test_empty_ports_list_returns_empty_array(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    """ExternalName / headless-without-ports services have no ports."""
    services_api.list_namespaced_service.return_value = SimpleNamespace(
        items=[_service("api", ports=[])]
    )

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.data["services"][0]["ports"] == []


# ---------------------------------------------------------------------------
# Defensive / errors / input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_missing_metadata_spec_status_does_not_crash(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    weird: Any = SimpleNamespace(metadata=None, spec=None, status=None)
    services_api.list_namespaced_service.return_value = SimpleNamespace(items=[weird])

    result = await list_services(
        ListServicesInput(namespace="default"), ctx=kube_context, settings=Settings()
    )

    assert result.success is True
    [out] = result.data["services"]
    assert out["name"] == "Unknown"
    assert out["namespace"] == "Unknown"
    assert out["type"] is None
    assert out["cluster_ip"] is None
    assert out["external_ip"] is None
    assert out["ports"] == []


@pytest.mark.asyncio
async def test_returns_error_on_api_exception(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.side_effect = ApiException(status=403, reason="Forbidden")

    result = await list_services(
        ListServicesInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "Forbidden" in (result.error or "")


@pytest.mark.asyncio
async def test_returns_error_on_unexpected_exception(
    kube_context: KubeContext, services_api: MagicMock
) -> None:
    services_api.list_namespaced_service.side_effect = RuntimeError("boom")

    result = await list_services(
        ListServicesInput(namespace="dev"), ctx=kube_context, settings=Settings()
    )

    assert result.success is False
    assert "boom" in (result.error or "")


@pytest.mark.parametrize(
    "payload",
    [
        {"extra": "nope"},
        {"limit": 0},
        {"limit": -1},
        {"limit": 1001},
    ],
)
def test_input_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ListServicesInput.model_validate(payload)
