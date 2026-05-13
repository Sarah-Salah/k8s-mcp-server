# k8s-mcp-server

An MCP server that lets AI assistants — Claude Desktop, Cursor, Claude Code — safely inspect and operate on Kubernetes clusters through natural conversation.

<!-- Badges placeholder: add once published to PyPI and CI is green -->
<!-- ![CI](https://github.com/<you>/k8s-mcp-server/actions/workflows/ci.yml/badge.svg) -->
<!-- ![PyPI](https://img.shields.io/pypi/v/k8s-mcp-server.svg) -->
<!-- ![Python](https://img.shields.io/pypi/pyversions/k8s-mcp-server.svg) -->

## Demo

<!-- Replace with a GIF or short video showing Claude Desktop diagnosing a pod -->
*Demo coming soon.*

## What it does

- 📋 List and inspect pods, deployments, services, nodes, events
- 📜 Stream pod logs
- 📊 View resource usage (top pods / top nodes)
- 🔍 Describe any standard Kubernetes resource
- ⚙️ Optionally scale deployments, rollout-restart, or delete pods — all behind a flag, all dry-run by default

All write operations are **off by default** and gated behind a `--enable-writes` flag. Writes default to `dry_run=True`. See [SECURITY.md](docs/SECURITY.md).

## Quick Start

### Install

```bash
# Run without installing (recommended)
uvx k8s-mcp-server

# Or install with pip
pip install k8s-mcp-server
```

You'll need a working `~/.kube/config` pointing to the cluster you want to inspect.

### Configure Claude Desktop

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

Ready-to-paste config files (read-only and writes-enabled variants) live in [`examples/`](examples/).

Restart Claude Desktop. You should now be able to ask things like:

> "List all pods in the staging namespace that aren't running."

> "Why is pod `api-7d4f9` crashing? Check its logs and recent events."

### Enable writes (use with caution)

```json
{
  "mcpServers": {
    "kubernetes": {
      "command": "uvx",
      "args": [
        "k8s-mcp-server",
        "--enable-writes",
        "--namespaces", "dev,staging"
      ]
    }
  }
}
```

With writes enabled you can also say:

> "Scale the `api` deployment in staging to 5 replicas."

> "Restart the rollout for `frontend`."

All write tools still default to `dry_run=True`. You (or the model) must explicitly confirm.

## Available Tools

| Tool                  | Type  | Description                                |
| --------------------- | ----- | ------------------------------------------ |
| `list_namespaces`     | read  | List namespaces                            |
| `list_pods`           | read  | List pods (filterable)                     |
| `get_pod`             | read  | Full pod state + recent events             |
| `get_pod_logs`        | read  | Pod logs (tail / since / previous)         |
| `list_deployments`    | read  | List deployments                           |
| `get_deployment`      | read  | Deployment + rollout history               |
| `list_services`       | read  | List services                              |
| `list_nodes`          | read  | List nodes + capacity                      |
| `get_node`            | read  | Full node state                            |
| `list_events`         | read  | Cluster events (filterable)                |
| `describe_resource`   | read  | Generic `describe` for standard kinds      |
| `top_pods`            | read  | Pod CPU / memory (needs metrics-server)    |
| `top_nodes`           | read  | Node CPU / memory                          |
| `scale_deployment`    | write | Scale a deployment                         |
| `restart_deployment`  | write | Rollout restart                            |
| `delete_pod`          | write | Delete a pod (it will be recreated)        |

Full details: [docs/TOOLS_SPEC.md](docs/TOOLS_SPEC.md).

## Configuration

| Flag                    | Default       | Description                                  |
| ----------------------- | ------------- | -------------------------------------------- |
| `--enable-writes`       | off           | Register write tools                         |
| `--namespaces ns1,ns2`  | all           | Restrict to specific namespaces              |
| `--kubeconfig PATH`     | `~/.kube/config` | Override kubeconfig path                  |
| `--context NAME`        | current       | Override kubeconfig context                  |
| `--log-level LEVEL`     | `INFO`        | `DEBUG` / `INFO` / `WARNING` / `ERROR`       |

## Security

This tool is designed with defense-in-depth. Writes are off by default, gated by a flag, default to dry-run, and emit audit log lines. Secrets are never returned. Read the full model in [docs/SECURITY.md](docs/SECURITY.md) before using `--enable-writes` against any cluster you care about.

## Development

```bash
git clone https://github.com/<you>/k8s-mcp-server
cd k8s-mcp-server
uv sync
uv run pytest
uv run ruff check
uv run mypy src/
```

Project conventions and workflow in [CLAUDE.md](CLAUDE.md).

## Roadmap

See [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md). Highlights for v2:
- HTTP / SSE transport
- In-cluster deployment with Helm chart
- ServiceAccount + RBAC
- Persistent audit log

## License

MIT