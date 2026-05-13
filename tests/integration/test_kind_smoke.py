"""Single end-to-end smoke test against a real Kubernetes cluster.

Skipped automatically unless ``KUBECONFIG`` is set. Opt in to run only the
integration test with::

    pytest -m integration

See ``docs/INTEGRATION_TESTING.md`` for kind cluster setup.
"""

from __future__ import annotations

import os

import pytest

from k8s_mcp_server.config import Settings
from k8s_mcp_server.kube.client import load_context
from k8s_mcp_server.tools.namespaces import ListNamespacesInput, list_namespaces
from k8s_mcp_server.tools.pods import GetPodInput, ListPodsInput, get_pod, list_pods

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("KUBECONFIG"),
        reason="KUBECONFIG env var not set — see docs/INTEGRATION_TESTING.md",
    ),
]


@pytest.mark.asyncio
async def test_kind_cluster_smoke() -> None:
    """End-to-end: real kubeconfig + 3 tools against a real K8s API server.

    Exercises the full stack:
        - ``load_context`` (kubeconfig parse + ApiClient construction)
        - ``list_namespaces`` (CoreV1Api.list_namespace via the registry path)
        - ``list_pods`` (CoreV1Api.list_namespaced_pod with a real namespace)
        - ``get_pod`` (read + events fetch + format pipeline)

    Uses ``kube-system`` because every cluster has system pods we can rely on
    (kube-apiserver, kube-controller-manager, kube-proxy, ...). No fixture
    pods to deploy.
    """
    settings = Settings()
    ctx = load_context(settings)

    # 1. Every cluster has at least default + kube-system.
    ns_result = await list_namespaces(ListNamespacesInput(), ctx=ctx, settings=settings)
    assert ns_result.success, ns_result.error
    names = {n["name"] for n in ns_result.data["namespaces"]}
    assert "default" in names
    assert "kube-system" in names

    # 2. kube-system always has system pods (kube-apiserver, kube-proxy, ...).
    pods_result = await list_pods(
        ListPodsInput(namespace="kube-system"), ctx=ctx, settings=settings
    )
    assert pods_result.success, pods_result.error
    assert len(pods_result.data["pods"]) > 0, "kube-system unexpectedly empty"

    # 3. Detail fetch on the first system pod — exercises the whole shape.
    first_pod_name = pods_result.data["pods"][0]["name"]
    detail = await get_pod(
        GetPodInput(name=first_pod_name, namespace="kube-system"),
        ctx=ctx,
        settings=settings,
    )
    assert detail.success, detail.error
    assert detail.data["name"] == first_pod_name
    assert detail.data["namespace"] == "kube-system"
    # Shape pins — these keys must be present even on a fresh cluster.
    for key in ("containers", "init_containers", "conditions", "events", "metadata"):
        assert key in detail.data, f"missing {key!r} in get_pod response"
