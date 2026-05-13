# Integration Testing

The unit tests (`pytest`, 528 of them) all use mocked Kubernetes clients.
This document covers the **single smoke test** in
`tests/integration/test_kind_smoke.py` that runs against a real cluster
end-to-end — the same test CI runs against every PR.

## Why one smoke test?

The unit tests prove tool *logic* is correct. The smoke test proves the
*plumbing* works: kubeconfig parsing, the `kubernetes` Python client against
a real API server, our `asyncio.to_thread` wrappers, the registry, and the
response shape. Three tool calls (`list_namespaces`, `list_pods`, `get_pod`)
on the `kube-system` namespace exercise the whole stack without needing
fixture pods deployed.

## Local setup

### 1. Install kind

**macOS:**

```bash
brew install kind
```

**Linux / Windows / other:** see
[kind installation](https://kind.sigs.k8s.io/docs/user/quick-start/#installation).

### 2. Create a test cluster

```bash
kind create cluster --name k8s-mcp-test
```

Takes ~30 seconds. Writes the cluster's kubeconfig into your default
`~/.kube/config` and switches the active context to `kind-k8s-mcp-test`.

### 3. Set KUBECONFIG

The integration test skips unless `KUBECONFIG` is set:

```bash
export KUBECONFIG="$HOME/.kube/config"
```

(Older `kind` versions used a per-cluster path obtained via
`$(kind get kubeconfig-path --name k8s-mcp-test)`; recent versions merge
into the default location, which is what the export above expects.)

### 4. Run

Only the integration test:

```bash
uv run pytest -m integration -v
```

Everything (unit + integration):

```bash
uv run pytest
```

In default `pytest` runs without `KUBECONFIG`, the integration test shows
as `1 skipped` — that's intentional. The visibility reminds developers the
test exists and what enables it.

### 5. Teardown

```bash
kind delete cluster --name k8s-mcp-test
```

## CI

The `integration-test` job in `.github/workflows/ci.yml` runs steps 1–4 on
every push and PR. It runs in **parallel** with the existing `lint-test`
(unit) job — they don't depend on each other so a unit-test failure
doesn't block the integration check, and vice versa.

## Troubleshooting

These three scenarios cover ~80% of integration-test debugging time.

### "test skipped despite KUBECONFIG set"

The skipif checks `os.environ.get("KUBECONFIG")` — non-empty string passes.
If the test still skips, verify the path actually resolves to a real file:

```bash
echo "$KUBECONFIG"
ls -la "$KUBECONFIG"
```

If the file is missing, recreate the kind cluster (step 2) — kind writes
the kubeconfig fresh on each `create`.

### `ConnectionRefused` or `"couldn't get current server API"`

The kind cluster died (Docker restart, system reboot, OOM). Verify with:

```bash
kubectl get nodes
```

If that fails, recreate the cluster:

```bash
kind delete cluster --name k8s-mcp-test
kind create cluster --name k8s-mcp-test
```

### `PermissionDenied` / `Forbidden`

kind clusters grant cluster-admin to the kubeconfig user by default, so
this shouldn't happen. If it does, the kubeconfig context is pointing at
the wrong cluster (a real one with restricted RBAC). Check with:

```bash
kubectl config current-context
kubectl auth can-i list pods --namespace kube-system
```

The current context should be `kind-k8s-mcp-test` and the auth check
should return `yes`.
