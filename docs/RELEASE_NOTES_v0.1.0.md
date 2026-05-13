# k8s-mcp-server 0.1.0

**Initial release.** k8s-mcp-server is a Model Context Protocol (MCP)
server that gives AI assistants — Claude Desktop, Cursor, Claude Code —
safe, structured access to Kubernetes clusters. Read tools work out of
the box and can't change cluster state; write tools live behind a flag
and default to dry-run. 16 tools, 528 unit tests + 1 kind-cluster
integration smoke test, 100% library coverage.

## What's in 0.1.0

### Read operations

13 tools covering the diagnostic surface area an AI assistant actually
needs: list/get/describe across pods, deployments, services, nodes,
namespaces, and events; pod logs with tail/since/previous; pod and node
metrics (CPU + memory) from `metrics.k8s.io` with quantity
normalization; and a polymorphic `describe_resource` tool across 7
kinds (Secret values are redacted at the summarizer level).

### Write operations

Three opt-in tools — `scale_deployment`, `restart_deployment`,
`delete_pod` — each gated by `--enable-writes` at server start, each
defaulting to `dry_run=True`, each audited on every attempt. The LLM
must explicitly pass `dry_run=False` to apply.

### Safety

Five layers of defense in depth (flag → registry filter → in-handler
re-check → dry-run-by-default → audit log) plus a
`--namespaces dev,staging` allowlist that limits blast radius for reads
AND writes. Secret values are never returned. Full threat model in
[`docs/SECURITY.md`](SECURITY.md).

## Installation

```bash
pip install k8s-mcp-server
```

Or run without installing:

```bash
uvx k8s-mcp-server --help
```

## Quick start

Paste the config snippet from
[`examples/claude_desktop_config.read_only.json`](../examples/claude_desktop_config.read_only.json)
into Claude Desktop's `mcpServers` block (see
[`examples/README.md`](../examples/README.md) for file locations) and
restart Claude Desktop. Full walk-through in the
[main README](../README.md).

## What's not in 0.1.0

These are on the v2 roadmap ([`docs/PROJECT_PLAN.md`](PROJECT_PLAN.md)):

- **HTTP / SSE transport** — 0.1.0 is stdio-only.
- **In-cluster deployment** — no Helm chart, no ServiceAccount-based
  auth. 0.1.0 runs locally with a kubeconfig.
- **Persistent audit log** — current audit goes to stderr only; no file
  or syslog sink.
- **`apply_manifest`** — no arbitrary YAML apply; the three write tools
  cover the narrow ops surface.
- **Custom Resource Definitions (CRDs)** — `describe_resource` is fixed
  to 7 standard kinds.
- **Multi-cluster / context-switching at runtime** — single cluster
  per server start.

## Acknowledgments

Built on top of:

- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — protocol layer
- [kubernetes-py client](https://github.com/kubernetes-client/python) — Kubernetes API client
- [pydantic v2](https://docs.pydantic.dev/) — input validation and tool schema generation

## Full changelog

See [`CHANGELOG.md`](../CHANGELOG.md#010---2026-05-13).
