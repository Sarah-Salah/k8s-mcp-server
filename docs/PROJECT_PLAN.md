# Project Plan

A production-quality MCP server giving AI assistants safe, observable access to Kubernetes clusters.

## v1 Goals

- 10+ read tools (list / get / describe / logs / events / metrics) against any cluster reachable via `kubectl`
- 3 write tools (scale, rollout restart, delete pod) gated behind `--enable-writes` and dry-run by default
- stdio transport, works out of the box with Claude Desktop, Cursor, and Claude Code
- Distributed via PyPI (`pip install k8s-mcp-server`) and `uvx`
- >85% test coverage on tool implementations
- CI on GitHub Actions

## Roadmap

### v1 (current)

- [x] Project skeleton & CI
- [ ] Read tools: pods, deployments, services, nodes, events, logs, metrics, describe
- [ ] Write tools: scale, restart, delete (with audit log)
- [ ] Documentation, demo, PyPI release

### v2 (future)

- HTTP / SSE transport
- In-cluster deployment with Helm chart
- ServiceAccount + RBAC, multi-cluster support
- Persistent audit log
- `apply_manifest` tool with policy-based safety

## Non-Goals (v1)

To keep v1 focused and shippable:

- No arbitrary YAML apply
- No RBAC resource manipulation
- No multi-cluster support
- No web UI
- Secrets `.data` is never returned, even on explicit request

See [SECURITY.md](SECURITY.md) for the full threat model.
