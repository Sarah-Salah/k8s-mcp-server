"""Tests for the ``describe_resource`` tool."""

from __future__ import annotations

import json
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
from k8s_mcp_server.tools.describe import DescribeResourceInput, describe_resource

MODULE = "k8s_mcp_server.tools.describe"


@pytest.fixture
def describe_apis(
    patch_core_v1: Callable[[str], MagicMock],
    patch_apps_v1: Callable[[str], MagicMock],
    patch_networking_v1: Callable[[str], MagicMock],
) -> SimpleNamespace:
    """All three API surfaces patched inside the describe module."""
    return SimpleNamespace(
        core=patch_core_v1(MODULE),
        apps=patch_apps_v1(MODULE),
        netw=patch_networking_v1(MODULE),
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _meta(
    *,
    name: str = "x",
    namespace: str | None = "dev",
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    uid: str = "uid-1",
    age_minutes: int = 60,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        namespace=namespace,
        uid=uid,
        creation_timestamp=datetime.now(UTC) - timedelta(minutes=age_minutes),
        labels=labels if labels is not None else {},
        annotations=annotations if annotations is not None else {},
    )


def _container(name: str, image: str = "nginx:1.25") -> SimpleNamespace:
    return SimpleNamespace(name=name, image=image)


def _condition(type_: str, status: str, *, last_minutes: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        type=type_,
        status=status,
        reason=None,
        message=None,
        last_transition_time=datetime.now(UTC) - timedelta(minutes=last_minutes),
    )


def _pod(name: str = "api", namespace: str = "dev") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=_meta(name=name, namespace=namespace),
        spec=SimpleNamespace(
            containers=[_container("app", "api:1.4")],
            node_name="node-3",
            restart_policy="Always",
        ),
        status=SimpleNamespace(phase="Running", pod_ip="10.0.0.1"),
    )


def _deployment(name: str = "api", namespace: str = "dev") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=_meta(name=name, namespace=namespace),
        spec=SimpleNamespace(
            replicas=5,
            strategy=SimpleNamespace(type="RollingUpdate"),
            selector=SimpleNamespace(match_labels={"app": "api"}),
            template=None,
        ),
        status=SimpleNamespace(ready_replicas=5, available_replicas=5),
    )


def _service(
    name: str = "api",
    namespace: str = "dev",
    *,
    type_: str = "ClusterIP",
    ingress: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=_meta(name=name, namespace=namespace),
        spec=SimpleNamespace(
            type=type_,
            cluster_ip="10.96.0.1",
            ports=[SimpleNamespace(port=80, protocol="TCP")],
        ),
        status=SimpleNamespace(load_balancer=SimpleNamespace(ingress=ingress)),
    )


def _node(
    name: str = "node-1",
    *,
    ready: str = "True",
    labels: dict[str, str] | None = None,
    taints: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=_meta(name=name, namespace=None, labels=labels),
        spec=SimpleNamespace(taints=taints),
        status=SimpleNamespace(
            conditions=[_condition("Ready", ready)],
            node_info=SimpleNamespace(kubelet_version="v1.28.3"),
            capacity={"cpu": "4", "memory": "16Gi"},
        ),
    )


def _configmap(
    name: str = "app-config",
    namespace: str = "dev",
    *,
    data: dict[str, str] | None = None,
    binary_data: dict[str, bytes] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=_meta(name=name, namespace=namespace),
        data=data if data is not None else {"app.yaml": "..."},
        binary_data=binary_data,
    )


def _secret(
    name: str = "db",
    namespace: str = "dev",
    *,
    type_: str = "Opaque",
    data: dict[str, str] | None = None,
    string_data: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=_meta(name=name, namespace=namespace, annotations=annotations),
        type=type_,
        data=data,
        string_data=string_data,
    )


def _ingress(
    name: str = "api",
    namespace: str = "dev",
    *,
    rules: list[SimpleNamespace] | None = None,
    ingress: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=_meta(name=name, namespace=namespace),
        spec=SimpleNamespace(rules=rules),
        status=SimpleNamespace(load_balancer=SimpleNamespace(ingress=ingress)),
    )


def _event(
    *,
    type_: str = "Normal",
    reason: str = "Pulled",
    message: str = "...",
    age_minutes: int = 5,
) -> SimpleNamespace:
    last = datetime.now(UTC) - timedelta(minutes=age_minutes)
    first = last - timedelta(minutes=1)
    return SimpleNamespace(
        type=type_,
        reason=reason,
        message=message,
        count=1,
        last_timestamp=last,
        event_time=None,
        first_timestamp=first,
        metadata=SimpleNamespace(creation_timestamp=first),
    )


# ---------------------------------------------------------------------------
# Kind validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_kind",
    ["Pod", "POD", "deploy", "serviceaccount", "unknown", ""],
)
def test_input_rejects_invalid_kind(bad_kind: str) -> None:
    with pytest.raises(ValidationError):
        DescribeResourceInput.model_validate({"kind": bad_kind, "name": "x"})


@pytest.mark.parametrize(
    "good_kind",
    ["pod", "deployment", "service", "node", "configmap", "secret", "ingress"],
)
def test_input_accepts_all_valid_kinds(good_kind: str) -> None:
    inp = DescribeResourceInput.model_validate({"kind": good_kind, "name": "x"})
    assert inp.kind == good_kind


def test_input_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        DescribeResourceInput.model_validate({"kind": "pod", "name": "x", "extra": 1})


def test_input_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        DescribeResourceInput.model_validate({"kind": "pod", "name": ""})


# ---------------------------------------------------------------------------
# Namespace handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespaced_kind_uses_default_when_namespace_none(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.return_value = _pod(namespace="default")
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="x"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert describe_apis.core.read_namespaced_pod.call_args.kwargs["namespace"] == "default"


@pytest.mark.asyncio
async def test_namespaced_kind_accepts_specific_namespace(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.return_value = _pod(namespace="staging")
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await describe_resource(
        DescribeResourceInput(kind="pod", name="x", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert describe_apis.core.read_namespaced_pod.call_args.kwargs["namespace"] == "staging"


@pytest.mark.asyncio
async def test_namespace_all_rejected(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="x", namespace="all"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "namespace='all' is not supported" in (result.error or "")
    describe_apis.core.read_namespaced_pod.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="x", namespace="prod"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev", "staging")),
    )

    assert result.success is False
    assert "prod" in (result.error or "")
    assert "allowlist" in (result.error or "")


@pytest.mark.asyncio
async def test_default_namespace_outside_allowlist_rejected(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="x"),
        ctx=kube_context,
        settings=Settings(namespaces=("dev",)),
    )

    assert result.success is False
    assert "default" in (result.error or "")
    assert "specify a namespace explicitly" in (result.error or "")


@pytest.mark.asyncio
async def test_cluster_scoped_kind_rejects_namespace_input(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    result = await describe_resource(
        DescribeResourceInput(kind="node", name="node-1", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "kind='node' does not accept a namespace parameter" in (result.error or "")
    describe_apis.core.read_node.assert_not_called()


@pytest.mark.asyncio
async def test_cluster_scoped_kind_accepts_no_namespace(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_node.return_value = _node()

    result = await describe_resource(
        DescribeResourceInput(kind="node", name="node-1"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["kind"] == "Node"
    assert result.data["namespace"] is None


# ---------------------------------------------------------------------------
# Per-kind happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_pod_returns_structured_state(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.return_value = _pod(name="api", namespace="dev")
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    data = result.data
    assert data["kind"] == "Pod"
    assert data["name"] == "api"
    assert data["namespace"] == "dev"
    assert data["spec_summary"] == {
        "containers": [{"name": "app", "image": "api:1.4"}],
        "node_name": "node-3",
        "restart_policy": "Always",
    }
    assert data["status"] == {"phase": "Running", "pod_ip": "10.0.0.1"}
    assert "labels" in data["metadata"]
    assert "annotations" in data["metadata"]
    assert data["metadata"]["uid"] == "uid-1"
    assert data["metadata"]["creation_timestamp"] is not None


@pytest.mark.asyncio
async def test_describe_deployment_returns_structured_state(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.apps.read_namespaced_deployment.return_value = _deployment()
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="deployment", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["kind"] == "Deployment"
    assert result.data["spec_summary"] == {
        "replicas": 5,
        "strategy": "RollingUpdate",
        "match_labels": {"app": "api"},
    }
    assert result.data["status"] == {"replicas_ready": 5, "replicas_available": 5}


@pytest.mark.asyncio
async def test_describe_service_returns_structured_state(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_service.return_value = _service(
        type_="LoadBalancer",
        ingress=[SimpleNamespace(ip="203.0.113.5", hostname=None)],
    )
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="service", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["kind"] == "Service"
    assert result.data["spec_summary"] == {
        "type": "LoadBalancer",
        "cluster_ip": "10.96.0.1",
        "ports": [{"port": 80, "protocol": "TCP"}],
    }
    assert result.data["status"]["load_balancer_ingress"] == [
        {"ip": "203.0.113.5", "hostname": None}
    ]


@pytest.mark.asyncio
async def test_describe_service_port_protocol_defaults_to_tcp(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """Defensive: spec.ports[i].protocol can be missing — coerce to TCP."""
    svc = _service()
    svc.spec.ports = [SimpleNamespace(port=443, protocol=None)]
    describe_apis.core.read_namespaced_service.return_value = svc
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="service", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["spec_summary"]["ports"] == [{"port": 443, "protocol": "TCP"}]


@pytest.mark.asyncio
async def test_describe_node_returns_structured_state(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_node.return_value = _node(
        name="node-1",
        labels={"node-role.kubernetes.io/worker": ""},
        taints=[
            SimpleNamespace(key="node.kubernetes.io/unreachable", value=None, effect="NoExecute")
        ],
    )

    result = await describe_resource(
        DescribeResourceInput(kind="node", name="node-1"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["kind"] == "Node"
    assert result.data["namespace"] is None
    assert result.data["spec_summary"]["kubelet_version"] == "v1.28.3"
    assert result.data["spec_summary"]["capacity"] == {"cpu": "4", "memory": "16Gi"}
    assert result.data["spec_summary"]["taints"] == [
        {"key": "node.kubernetes.io/unreachable", "value": None, "effect": "NoExecute"}
    ]
    assert result.data["status"] == {"ready_status": "Ready"}
    assert result.data["events"] == []


@pytest.mark.asyncio
async def test_describe_node_ready_status_derivation(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """The Node summarizer derives Ready/NotReady/Unknown from conditions."""
    n = _node()
    n.status.conditions = [_condition("Ready", "False")]
    describe_apis.core.read_node.return_value = n

    result = await describe_resource(
        DescribeResourceInput(kind="node", name="node-1"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["status"]["ready_status"] == "NotReady"


@pytest.mark.asyncio
async def test_describe_node_ready_status_unknown_when_no_ready_condition(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    n = _node()
    n.status.conditions = [_condition("MemoryPressure", "False")]
    describe_apis.core.read_node.return_value = n

    result = await describe_resource(
        DescribeResourceInput(kind="node", name="node-1"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["status"]["ready_status"] == "Unknown"


@pytest.mark.asyncio
async def test_describe_node_ready_status_unknown_when_status_unknown(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    n = _node(ready="Unknown")
    describe_apis.core.read_node.return_value = n

    result = await describe_resource(
        DescribeResourceInput(kind="node", name="node-1"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["status"]["ready_status"] == "Unknown"


@pytest.mark.asyncio
async def test_describe_configmap_returns_data_keys_only(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_config_map.return_value = _configmap(
        data={"app.yaml": "secret_app_config_content", "log.conf": "level=DEBUG"},
        binary_data={"cert.bin": b"\\x00\\x01"},
    )

    result = await describe_resource(
        DescribeResourceInput(kind="configmap", name="app-config", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["kind"] == "ConfigMap"
    assert result.data["spec_summary"]["data_keys"] == ["app.yaml", "log.conf"]
    assert result.data["spec_summary"]["binary_data_keys"] == ["cert.bin"]
    assert result.data["status"] == {}
    # Events skipped for ConfigMap
    assert result.data["events"] == []
    describe_apis.core.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_describe_ingress_returns_rules_summary(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    path_numeric = SimpleNamespace(
        path="/api",
        path_type="Prefix",
        backend=SimpleNamespace(
            service=SimpleNamespace(
                name="api-svc",
                port=SimpleNamespace(number=8080, name=None),
            )
        ),
    )
    path_named = SimpleNamespace(
        path="/web",
        path_type="Prefix",
        backend=SimpleNamespace(
            service=SimpleNamespace(
                name="web-svc",
                port=SimpleNamespace(number=None, name="http"),
            )
        ),
    )
    rule = SimpleNamespace(
        host="example.com",
        http=SimpleNamespace(paths=[path_numeric, path_named]),
    )
    describe_apis.netw.read_namespaced_ingress.return_value = _ingress(
        rules=[rule], ingress=[SimpleNamespace(ip="203.0.113.7", hostname=None)]
    )
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="ingress", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["kind"] == "Ingress"
    assert result.data["spec_summary"]["rules"] == [
        {
            "host": "example.com",
            "paths": [
                {
                    "path": "/api",
                    "path_type": "Prefix",
                    "backend": {"service_name": "api-svc", "service_port": 8080},
                },
                {
                    "path": "/web",
                    "path_type": "Prefix",
                    "backend": {"service_name": "web-svc", "service_port": "http"},
                },
            ],
        }
    ]
    assert result.data["status"]["load_balancer_ingress"] == [
        {"ip": "203.0.113.7", "hostname": None}
    ]


@pytest.mark.asyncio
async def test_describe_ingress_with_missing_backend_service(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """Defensive: an Ingress path with no backend.service → service_name/port both None."""
    path = SimpleNamespace(path="/", path_type="Prefix", backend=SimpleNamespace(service=None))
    rule = SimpleNamespace(host=None, http=SimpleNamespace(paths=[path]))
    describe_apis.netw.read_namespaced_ingress.return_value = _ingress(rules=[rule])
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="ingress", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["spec_summary"]["rules"][0]["paths"][0]["backend"] == {
        "service_name": None,
        "service_port": None,
    }


@pytest.mark.asyncio
async def test_describe_ingress_with_service_but_no_port(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """Defensive: Service backend with port=None → service_port=None."""
    path = SimpleNamespace(
        path="/",
        path_type="Prefix",
        backend=SimpleNamespace(service=SimpleNamespace(name="svc", port=None)),
    )
    rule = SimpleNamespace(host=None, http=SimpleNamespace(paths=[path]))
    describe_apis.netw.read_namespaced_ingress.return_value = _ingress(rules=[rule])
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="ingress", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["spec_summary"]["rules"][0]["paths"][0]["backend"] == {
        "service_name": "svc",
        "service_port": None,
    }


@pytest.mark.asyncio
async def test_describe_ingress_with_port_neither_number_nor_name(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """Defensive: Service backend with port object but both number and name None → None."""
    path = SimpleNamespace(
        path="/",
        path_type="Prefix",
        backend=SimpleNamespace(
            service=SimpleNamespace(name="svc", port=SimpleNamespace(number=None, name=None))
        ),
    )
    rule = SimpleNamespace(host=None, http=SimpleNamespace(paths=[path]))
    describe_apis.netw.read_namespaced_ingress.return_value = _ingress(rules=[rule])
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="ingress", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["spec_summary"]["rules"][0]["paths"][0]["backend"]["service_port"] is None


# ---------------------------------------------------------------------------
# Secret (SECURITY-CRITICAL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_secret_redacts_data_values(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """SECURITY-CRITICAL: Secret values must never appear in the response.

    Pin this behaviour by serializing the whole response to JSON and asserting
    the base64-encoded payload bytes are nowhere in it.
    """
    describe_apis.core.read_namespaced_secret.return_value = _secret(
        type_="Opaque",
        data={"password": "c2VjcmV0X3Bhc3N3b3Jk", "api-key": "YWJjMTIz"},
        string_data={"raw_token": "plaintext_token_value"},
    )

    result = await describe_resource(
        DescribeResourceInput(kind="secret", name="db", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    serialized = json.dumps(result.data)
    assert "c2VjcmV0X3Bhc3N3b3Jk" not in serialized
    assert "YWJjMTIz" not in serialized
    assert "plaintext_token_value" not in serialized
    assert result.data["spec_summary"]["type"] == "Opaque"
    assert result.data["spec_summary"]["data_keys"] == ["api-key", "password", "raw_token"]
    assert result.data["status"] == {}
    # Events skipped for Secret
    assert result.data["events"] == []
    describe_apis.core.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_describe_secret_strips_last_applied_configuration_annotation(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """SECURITY-CRITICAL: ``kubectl.kubernetes.io/last-applied-configuration``
    embeds the full applied JSON, which for a ``kubectl apply -f`` of a Secret
    includes the base64-encoded ``.data`` block. The annotation MUST be
    stripped from the response.

    This test pins the specific vulnerability — a code change that
    re-introduced it would fail here loudly.
    """
    leaky_payload = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "db"},
            "type": "Opaque",
            "data": {"password": "c2VjcmV0X3Bhc3N3b3Jk"},
        }
    )
    describe_apis.core.read_namespaced_secret.return_value = _secret(
        type_="Opaque",
        data={"password": "c2VjcmV0X3Bhc3N3b3Jk"},
        annotations={
            "kubectl.kubernetes.io/last-applied-configuration": leaky_payload,
            "harmless": "this should still appear",
        },
    )

    result = await describe_resource(
        DescribeResourceInput(kind="secret", name="db", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    serialized = json.dumps(result.data)
    # The base64 value must NOT appear via either spec data OR the annotation
    assert "c2VjcmV0X3Bhc3N3b3Jk" not in serialized
    # The leaky annotation key itself is gone
    assert "last-applied-configuration" not in serialized
    # Other annotations pass through unchanged
    assert result.data["metadata"]["annotations"] == {"harmless": "this should still appear"}


@pytest.mark.asyncio
async def test_describe_secret_with_no_data_returns_empty_keys(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_secret.return_value = _secret(
        type_="kubernetes.io/service-account-token", data=None, string_data=None
    )

    result = await describe_resource(
        DescribeResourceInput(kind="secret", name="sa-token", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["spec_summary"]["data_keys"] == []
    assert result.data["spec_summary"]["type"] == "kubernetes.io/service-account-token"


@pytest.mark.asyncio
async def test_non_secret_kinds_keep_last_applied_configuration_annotation(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """The annotation strip is SECRET-ONLY — for other kinds it's harmless metadata."""
    pod = _pod()
    pod.metadata = _meta(
        name="api",
        namespace="dev",
        annotations={"kubectl.kubernetes.io/last-applied-configuration": "{...}"},
    )
    describe_apis.core.read_namespaced_pod.return_value = pod
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert (
        "kubectl.kubernetes.io/last-applied-configuration" in result.data["metadata"]["annotations"]
    )


# ---------------------------------------------------------------------------
# 404 / non-404 / unexpected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_friendly_error_for_namespaced_kind(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="ghost", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert result.error == "pod 'ghost' not found in namespace 'staging'"
    describe_apis.core.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_404_friendly_error_for_cluster_scoped_kind(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_node.side_effect = ApiException(status=404, reason="Not Found")

    result = await describe_resource(
        DescribeResourceInput(kind="node", name="ghost-node"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert result.error == "node 'ghost-node' not found"


@pytest.mark.asyncio
async def test_non_404_api_error_returns_kubernetes_api_error(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.side_effect = ApiException(status=500, reason="Internal")

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="x", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "kubernetes API error" in (result.error or "")
    assert "Internal" in (result.error or "")


@pytest.mark.asyncio
async def test_unexpected_exception_returns_error(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.side_effect = RuntimeError("boom")

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="x", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is False
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_field_selector_uses_kind_and_name(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.return_value = _pod(name="api-1", namespace="staging")
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    await describe_resource(
        DescribeResourceInput(kind="pod", name="api-1", namespace="staging"),
        ctx=kube_context,
        settings=Settings(),
    )

    kwargs = describe_apis.core.list_namespaced_event.call_args.kwargs
    assert kwargs["namespace"] == "staging"
    assert kwargs["field_selector"] == "involvedObject.kind=Pod,involvedObject.name=api-1"


@pytest.mark.asyncio
async def test_events_capped_at_five(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.return_value = _pod()
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(
        items=[_event(reason=f"R{i}", age_minutes=i + 1) for i in range(8)]
    )

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    events = result.data["events"]
    assert len(events) == 5
    # Most-recent first
    assert events[0]["reason"] == "R0"
    assert events[-1]["reason"] == "R4"


@pytest.mark.asyncio
async def test_event_fetch_failure_returns_empty_events_with_success(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.return_value = _pod(name="api")
    describe_apis.core.list_namespaced_event.side_effect = ApiException(
        status=403, reason="Forbidden"
    )

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.error is None
    assert result.data["events"] == []
    assert result.data["name"] == "api"


@pytest.mark.asyncio
async def test_event_fetch_unexpected_exception_returns_empty_events(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_pod.return_value = _pod()
    describe_apis.core.list_namespaced_event.side_effect = RuntimeError("ev boom")

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["events"] == []


@pytest.mark.asyncio
async def test_events_skipped_for_secret(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_namespaced_secret.return_value = _secret()

    result = await describe_resource(
        DescribeResourceInput(kind="secret", name="db", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    assert result.data["events"] == []
    describe_apis.core.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_events_skipped_for_node_cluster_scoped(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.core.read_node.return_value = _node()

    result = await describe_resource(
        DescribeResourceInput(kind="node", name="node-1"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.data["events"] == []
    describe_apis.core.list_namespaced_event.assert_not_called()


@pytest.mark.asyncio
async def test_events_fetched_for_ingress(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    describe_apis.netw.read_namespaced_ingress.return_value = _ingress(rules=[])
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[_event()])

    result = await describe_resource(
        DescribeResourceInput(kind="ingress", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert len(result.data["events"]) == 1
    describe_apis.core.list_namespaced_event.assert_called_once()
    kwargs = describe_apis.core.list_namespaced_event.call_args.kwargs
    assert kwargs["field_selector"] == "involvedObject.kind=Ingress,involvedObject.name=api"


@pytest.mark.asyncio
async def test_event_with_only_event_time_sorts_correctly(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    """Defensive: events with event_time but no last_timestamp still sort + format."""
    older = _event(reason="Old", age_minutes=10)
    older.last_timestamp = None
    older.event_time = datetime.now(UTC) - timedelta(minutes=10)
    newer = _event(reason="New", age_minutes=2)
    newer.last_timestamp = None
    newer.event_time = datetime.now(UTC) - timedelta(minutes=2)
    describe_apis.core.read_namespaced_pod.return_value = _pod()
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[older, newer])

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="api", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert [e["reason"] for e in result.data["events"]] == ["New", "Old"]


# ---------------------------------------------------------------------------
# Defensive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_pod_with_missing_metadata_spec_status_does_not_crash(
    kube_context: KubeContext, describe_apis: SimpleNamespace
) -> None:
    weird: Any = SimpleNamespace(metadata=None, spec=None, status=None)
    describe_apis.core.read_namespaced_pod.return_value = weird
    describe_apis.core.list_namespaced_event.return_value = SimpleNamespace(items=[])

    result = await describe_resource(
        DescribeResourceInput(kind="pod", name="x", namespace="dev"),
        ctx=kube_context,
        settings=Settings(),
    )

    assert result.success is True
    data = result.data
    assert data["name"] == "Unknown"
    assert data["namespace"] is None
    assert data["metadata"]["labels"] == {}
    assert data["metadata"]["annotations"] == {}
    assert data["metadata"]["uid"] is None
    assert data["metadata"]["creation_timestamp"] is None
    assert data["spec_summary"]["containers"] == []
    assert data["status"]["phase"] is None
