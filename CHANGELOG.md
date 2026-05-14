# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `tests/integration/test_kind_smoke.py`: removed non-existent `metadata`
  key from the `get_pod` shape-assertion tuple. The `get_pod` tool
  flattens metadata fields (`name`, `namespace`, `age_*`) into the
  top level of the response — there is no nested `metadata` dict. This
  was a test-only bug caught on the first CI integration run; production
  code is unchanged.

### Changed

- `pyproject.toml`: prepared for v0.1.0 PyPI publish — added `Changelog`
  URL to `[project.urls]`, added `llm` and `devops` keywords, added
  `Environment :: Console` and `Typing :: Typed` classifiers.

### Added

- `src/k8s_mcp_server/py.typed` marker file so the `Typing :: Typed`
  classifier delivers actual value to downstream type-checkers (mypy,
  pyright). Empty file per PEP 561.
- `docs/RELEASE_CHECKLIST.md`: full publish runbook with sanity checks
  (incl. GitHub-username verification and the local kind integration
  test), build verification, TestPyPI publish (Phase B), real PyPI
  publish (Phase C), git tag + GitHub release (Phase D), and a rollback
  plan with the explicit "PyPI does not allow re-uploading a yanked
  version" warning. Documents `uv publish` as primary with
  `twine upload` as fallback.

## [0.1.0] - 2026-05-13

Initial release. 16 tools (13 read + 3 write), 528 unit tests + 1
kind-cluster integration smoke test, 100% library coverage.

### Added — Core tools

**Read operations (13 tools):**

- `list_namespaces` — list cluster namespaces with status and age
- `list_pods` — list pods with namespace/label/field-selector filters
- `get_pod` — single pod with container statuses, conditions, recent events
- `get_pod_logs` — pod logs with tail/since/previous and byte-cap
- `list_deployments` — list deployments with replicas and primary image
- `get_deployment` — full deployment state with last-5 rollout history
- `list_services` — list services with ports and LoadBalancer external IP
- `list_nodes` — list nodes with health, roles, kubelet version, capacity
- `get_node` — full node detail with conditions, taints, pods-on-node count
- `list_events` — cluster events filtered by kind/name/type/since
- `describe_resource` — polymorphic describe across 7 kinds (Secret redacted)
- `top_pods` — pod CPU/memory from `metrics.k8s.io` with quantity parsing
- `top_nodes` — node CPU/memory with percent against allocatable

**Write operations (3 tools — registered only with `--enable-writes`):**

- `scale_deployment` — set replica count via the `/scale` sub-resource
- `restart_deployment` — kubectl-compatible rollout restart annotation
- `delete_pod` — graceful or `force=True` immediate-kill delete

Full input/output specs in [`docs/TOOLS_SPEC.md`](docs/TOOLS_SPEC.md).

### Added — Infrastructure

- **Tool registry** with `ToolResult` envelope and `@register_tool` decorator
  (carries the `is_write` flag that Layer 2 filtering keys off).
- **`KubeContext` and `load_context()`** — kubeconfig loader honouring
  `--kubeconfig` and `--context`, reads the active context's default
  namespace, never echoes config contents.
- **Namespace allowlist** — `resolve_read_namespaces()` and
  `NamespaceNotAllowedError` in `kube/safe.py`, driven by
  `--namespaces ns1,ns2`. `"all"` resolves to the allowlist (never bypasses
  it).
- **Write Tool Contract** — `assert_writes_enabled()` (Layer 3 in-handler
  check) plus `dry_run=True` default plus audit logging. Documented in
  CLAUDE.md §6.1.
- **Audit logger** at the stable name `k8s_mcp_server.audit` with the
  `write_operation` prefix and field-name-based redaction for sensitive
  values (token / secret / password / api[_-]?key / bearer). The logger
  name is part of the public contract for ops integrations.
- **Shared helpers in `utils/`** — `event_sort_key` (multi-fallback
  timestamp resolution for K8s events) and `format_condition`
  (LLM-friendly trim of V1*Condition objects), used by both pod and
  deployment / event tools.
- **MCP stdio server bootstrap** (`server.py`, `__main__.py`) — async
  dispatcher with pydantic input validation, structured response
  envelopes, graceful `KubeConfigError` exit code 2.
- **CLI parser** (`config.py`) supporting `--enable-writes`,
  `--namespaces`, `--kubeconfig`, `--context`, `--log-level`, `--version`.

### Added — Documentation

- [`docs/TOOLS_SPEC.md`](docs/TOOLS_SPEC.md) — single source of truth for
  every tool's inputs, outputs, and behaviour invariants.
- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model and the 5-layer
  defense table.
- [`docs/PROJECT_PLAN.md`](docs/PROJECT_PLAN.md) — v1 scope and v2 roadmap.
- [`docs/INTEGRATION_TESTING.md`](docs/INTEGRATION_TESTING.md) — kind
  setup, run commands, Troubleshooting.
- [`CLAUDE.md`](CLAUDE.md) — working agreement, code conventions, and the
  **Write Tool Contract §6.1** (mandatory contract for every
  `is_write=True` tool).
- [`examples/`](examples/) — ready-to-paste Claude Desktop config files
  (read-only and writes-enabled variants) plus an `examples/README.md`
  with merge instructions and security warnings.
- [`README.md`](README.md) — production-ready with badges, Read/Write
  feature tables, Security Model, Architecture, Quick Start, and
  Acknowledgments.

### Added — Quality

- **528 unit tests** with mocked Kubernetes clients across every tool,
  helper, and CLI surface.
- **100% coverage** on library code (`tools/`, `kube/`, `utils/`,
  `config.py`, registry). The `__main__.py` and `server.py` plumbing is
  exercised end-to-end only.
- **Kind-cluster integration smoke test**
  ([`tests/integration/test_kind_smoke.py`](tests/integration/test_kind_smoke.py))
  exercising the full stack against a real API server via three tool
  calls. Skipped automatically unless `KUBECONFIG` is set.
- **GitHub Actions CI** with two **parallel** jobs: `lint-test` runs
  `ruff format --check`, `ruff check`, `mypy --strict`, and `pytest`;
  `integration-test` uses `helm/kind-action@v1` and runs
  `pytest -m integration`.
- **`pyproject.toml`** with majors pinned on `mcp` / `kubernetes` /
  `pydantic`, hatchling build backend, console-script entry point, and
  registered `integration` pytest marker.
- Two minor internal duplications (`_format_describe_event` slim variant
  of `_format_event`; `_ready_status_from_conditions` matching
  `tools/nodes.py`) intentionally left in place per the rule of three —
  see the `# DUPLICATION:` comments at the call sites for the extraction
  trigger.

### Security

- **Layer 1** — `--enable-writes` flag required at server start to
  register any write tool.
- **Layer 2** — `_visible_tools()` filter in server bootstrap excludes
  `is_write=True` tools from the MCP `list_tools` response when the flag
  is off (they aren't even visible to the LLM).
- **Layer 3** — every write handler calls `assert_writes_enabled()` as
  its first line, catching a hypothetical Layer 2 bypass.
- **Layer 4** — `dry_run=True` is the default on every write tool; the
  LLM must explicitly pass `dry_run=False` to apply. Server-side dry-run
  via the K8s API's `dry_run="All"` parameter.
- **Layer 5** — every write attempt audited at INFO on
  `k8s_mcp_server.audit` with tool name, target, `dry_run` value, and
  tool-specific deltas. Failed-PATCH paths still audited (no asymmetry).
- **Secret values never returned.** `describe_resource(kind="secret")`
  surfaces only key names. The
  `kubectl.kubernetes.io/last-applied-configuration` annotation is
  stripped from Secret responses to prevent annotation-based leaks of
  the base64-encoded `.data` block.
- **`force=True` in `delete_pod`** must appear in the audit log line so
  post-incident reviewers can grep for immediate-kill events. Pinned by
  a dedicated test
  (`test_delete_pod_audit_log_includes_force_true_field_for_post_incident_grep`).
- **`--namespaces` allowlist** limits blast radius for both reads and
  writes regardless of `--enable-writes`. `"all"` resolves to the
  allowlist, never bypasses.
- **Audit redaction** is field-name-based (not entropy-based): the regex
  `(?i)\b(token|secret|password|api[_-]?key|bearer)(\s*[=:]\s*)\S+`
  redacts the value while preserving the field name. Documented in
  `utils/audit.py` and pinned by parametrized tests.

[Unreleased]: https://github.com/Sarah-Salah/k8s-mcp-server/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Sarah-Salah/k8s-mcp-server/releases/tag/v0.1.0
