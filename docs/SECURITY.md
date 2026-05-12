# Security Model

## Threat Model

This server runs locally (v1) and exposes Kubernetes operations to an AI assistant. The AI assistant is **not trusted** — we treat its tool calls as if they come from a well-intentioned but unpredictable user.

Risks we mitigate:

| Risk                                                       | Mitigation                                     |
| ---------------------------------------------------------- | ---------------------------------------------- |
| Accidental destructive write                               | Writes off by default; `--enable-writes` flag  |
| Write to a sensitive namespace (e.g. `kube-system`)        | Optional `--namespaces` allowlist              |
| LLM hallucinates a write and applies it                    | All writes default to `dry_run=True`           |
| Secrets leak through tool responses                        | Tools never return Secret `.data`              |
| kubeconfig contents leak via logs / error messages         | kubeconfig content is never echoed             |
| Server runs longer than intended with writes enabled       | No runtime toggle — writes require restart     |
| Lost audit trail                                           | Every write emits a structured audit log line  |

## Defense in Depth

The server applies five layers of protection. A write tool only executes if **every** layer passes.

### Layer 1 — Server start flags

- `--enable-writes` is required to enable write tools at all
- `--read-only` short-circuits to always-read-only regardless of other flags
- `--namespaces ns1,ns2` (optional) restricts every tool — read and write — to those namespaces

### Layer 2 — Tool registration

- Write tools are **not registered** with the MCP server unless `--enable-writes` was set
- The LLM cannot call a tool that isn't registered — this is the strongest layer

### Layer 3 — Tool-body re-check

- Every write tool re-checks the writes-enabled flag at call time
- If somehow registered when it shouldn't be (a bug), it still refuses

### Layer 4 — Dry-run by default

- All write tools accept `dry_run: bool = True`
- Dry-run uses the K8s API's native `dryRun=All` parameter — it validates without applying
- The LLM must explicitly pass `dry_run=False` to apply, which is visible in audit logs

### Layer 5 — Audit log

- Every write attempt (dry-run or applied) logs to stderr with: timestamp, tool name, parameters, dry_run flag, result, calling user (kubeconfig context)
- v2: persist the audit log to a file or syslog

## Sensitive Data Handling

- **Secrets:** `list_secrets`-style operations and `describe_resource(kind="secret")` return only `name, namespace, type, age, data_keys` — never `.data` or `.stringData` values.
- **ConfigMaps:** Return only `name, namespace, age, keys` by default. Full data only via an explicit `include_data=True` parameter, which the LLM must request consciously.
- **kubeconfig:** Never echoed in any tool response. Tools may return the current **context name** only.
- **Log redaction:** The audit logger redacts patterns matching `(?i)(token|secret|password|api[_-]?key|bearer)\s*[=:]\s*\S+` before emitting.

## Identity & Auth

- **v1:** Whatever kubeconfig the server starts with is what it uses. No re-auth, no impersonation.
- **v2:** ServiceAccount in-cluster, with a minimal Role / ClusterRole shipped as part of the Helm chart.

## What This Server Will Never Do (v1)

- Apply arbitrary YAML manifests
- Create or modify RBAC resources
- Read or write Secret `.data` values
- Switch kubeconfig contexts at runtime
- Write to namespaces outside the allowlist (when set)
- Persist audit data beyond the current process's stderr

## Recommended User Workflow

For day-to-day debugging:

```bash
uvx k8s-mcp-server
```

For occasional, scoped operational work:

```bash
uvx k8s-mcp-server --enable-writes --namespaces dev,staging
```

For incident response in production: configure a separate, audited workflow. This tool is **not** intended as a primary production-write surface in v1.
