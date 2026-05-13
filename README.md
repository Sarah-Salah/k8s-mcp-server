# k8s-mcp-server

An MCP server that lets AI assistants — Claude Desktop, Cursor, Claude Code — safely inspect and operate on Kubernetes clusters through natural conversation.

![CI](https://github.com/<sarah-salah>/k8s-mcp-server/actions/workflows/ci.yml/badge.svg)
![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)
![PyPI](https://img.shields.io/pypi/v/k8s-mcp-server.svg)

## What it does

`k8s-mcp-server` gives Claude (and any other MCP-compatible AI assistant) safe, read-only access to Kubernetes clusters by default — list pods, tail logs, inspect deployments, view metrics, and describe any resource through natural conversation. Optional write operations (scale, restart, delete) live behind an `--enable-writes` flag and default to dry-run, so the LLM can't accidentally change cluster state.

Built with 528 unit tests against mocked K8s APIs plus a kind-cluster integration smoke test on every CI run — 100% library coverage.

## Demo

<!-- TODO: demo GIF, added in Step 4 -->

*Demo coming: a 30-second loop of Claude Desktop diagnosing a crashing pod — the model calls `list_pods`, spots `CrashLoopBackOff`, fetches `get_pod_logs` with `previous=True`, reads the stack trace, and suggests the fix.*

## Features

### Read operations (13 tools)

| Tool | What it does |
| --- | --- |
| `list_namespaces` | List all namespaces with status and age. |
| `list_pods` | List pods, filterable by namespace, labels, or field selectors. |
| `get_pod` | Single pod's full state — container statuses, conditions, recent events. |
| `get_pod_logs` | Pod logs with `tail_lines`, `since_seconds`, and `previous` (post-crash). |
| `list_deployments` | List deployments with replica counts and primary container image. |
| `get_deployment` | Full deployment state plus the last 5 ReplicaSets (rollout history). |
| `list_services` | List services with ports and LoadBalancer external IPs. |
| `list_nodes` | List nodes with health, roles, kubelet version, capacity. |
| `get_node` | Full node detail with conditions, taints, and pods-on-node count. |
| `list_events` | Cluster events filtered by kind/name/type/since, most recent first. |
| `describe_resource` | Structured `describe` view across 7 kinds (Secret values redacted). |
| `top_pods` | Pod CPU/memory usage (requires metrics-server). |
| `top_nodes` | Node CPU/memory usage with percent against allocatable. |

### Write operations (3 tools — require `--enable-writes`)

| Tool | What it does |
| --- | --- |
| `scale_deployment` | Set the replica count of a deployment via the `/scale` sub-resource. |
| `restart_deployment` | Trigger a rollout restart (kubectl-compatible annotation). |
| `delete_pod` | Delete a pod, optionally with `force=True` for immediate kill. |

Every write tool defaults to `dry_run=True`. Full input/output specs in [`docs/TOOLS_SPEC.md`](docs/TOOLS_SPEC.md).

## Installation

**Option A — try without installing (recommended for first run):**

```bash
uvx k8s-mcp-server --help
```

**Option B — install for daily use:**

```bash
pip install k8s-mcp-server
```

You'll need a working `~/.kube/config` pointing at the cluster you want to inspect.

## Quick Start (Claude Desktop)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%/Claude/claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "kubernetes": {
      "command": "uvx",
      "args": ["k8s-mcp-server"]
    }
  }
}
```

Restart Claude Desktop. You should now be able to ask:

> "List all pods in the staging namespace that aren't running."

> "Why is pod `api-7d4f9` crashing? Check its logs and recent events."

## Examples

Ready-to-paste config snippets (read-only and writes-enabled variants) live in [`examples/`](examples/) — see [`examples/README.md`](examples/README.md) for setup notes.

## Security Model

All write operations are **off by default**. The `--enable-writes` flag at server start is required to register them at all (Layer 1 + 2 of defense-in-depth). Once enabled, every write tool re-checks the flag at handler entry (Layer 3) and defaults to `dry_run=True` (Layer 4) — the LLM must explicitly pass `dry_run=False` to apply. The optional `--namespaces dev,staging` allowlist limits the blast radius for both reads and writes regardless of the flag. Every write attempt is audited at INFO level on the `k8s_mcp_server.audit` logger with the tool name, target, `dry_run` value, and tool-specific deltas (Layer 5).

Secret values are never returned — even via `describe_resource(kind="secret")`, only key names surface. The `kubectl.kubernetes.io/last-applied-configuration` annotation is stripped from Secret responses to prevent annotation-based leaks.

Read [`docs/SECURITY.md`](docs/SECURITY.md) for the full threat model and the layered defense table.

## Configuration

| Flag                    | Default          | Description                                  |
| ----------------------- | ---------------- | -------------------------------------------- |
| `--enable-writes`       | off              | Register write tools                         |
| `--namespaces ns1,ns2`  | all              | Restrict to specific namespaces              |
| `--kubeconfig PATH`     | `~/.kube/config` | Override kubeconfig path                     |
| `--context NAME`        | current          | Override kubeconfig context                  |
| `--log-level LEVEL`     | `INFO`           | `DEBUG` / `INFO` / `WARNING` / `ERROR`       |

## Architecture

Tools are registered via a small dataclass-based registry; each tool is a single async function that returns a structured `ToolResult(success, data, error, audit)` envelope and never raises into the MCP layer. Read tools defer to a shared namespace allowlist resolver (`resolve_read_namespaces`) and per-kind formatters in `tools/`. Write tools follow a strict three-layer defense pattern (CLI flag → server-level registry filter → in-handler `assert_writes_enabled`) on top of dry-run-by-default and audit logging. The polymorphic `describe_resource` tool dispatches via a per-kind table covering pod / deployment / service / node / configmap / secret / ingress.

See [`CLAUDE.md`](CLAUDE.md) §6.1 for the Write Tool Contract.

## Development

```bash
git clone https://github.com/<sarah-salah>/k8s-mcp-server
cd k8s-mcp-server
uv sync
uv run pytest
uv run ruff check
uv run mypy src/
```

Project conventions and workflow in [CLAUDE.md](CLAUDE.md). Integration tests against a real `kind` cluster are documented in [`docs/INTEGRATION_TESTING.md`](docs/INTEGRATION_TESTING.md).

## Roadmap (v2)

See [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md). Highlights for v2:

- HTTP / SSE transport
- In-cluster deployment with Helm chart
- ServiceAccount + RBAC
- Persistent audit log

## License & Acknowledgments

MIT — see [`LICENSE`](LICENSE).

Built on top of:

- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — the protocol layer
- [kubernetes-py client](https://github.com/kubernetes-client/python) — the API client
- [pydantic v2](https://docs.pydantic.dev/) — input validation and tool schema generation
