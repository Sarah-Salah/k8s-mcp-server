# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `tools/pods.py`: `get_pod` tool. Returns full pod state — name, namespace,
  phase, node, pod_ip, age, containers, init containers, conditions, and the
  10 most recent events (sorted by `last_timestamp`). Conditions include
  `last_transition_age_seconds` so the LLM can reason about how long a
  condition has been in its state. Container `state` is a nested dict
  `{phase, reason, message}` covering running / waiting / terminated.
- `get_pod` namespace handling: rejects `namespace="all"` upfront with a clear
  message; otherwise defers to `resolve_read_namespaces` (so the allowlist
  applies the same way as `list_pods`).
- `get_pod` events: separate `list_namespaced_event` call with
  `field_selector="involvedObject.kind=Pod,involvedObject.name=<name>"`.
  UID-based filtering is intentionally NOT used because it would drop
  kubelet-emitted events whose `involvedObject.uid` is null.
- `get_pod` 404 handling: returns `success=False` with
  `"pod 'X' not found in namespace 'Y'"` rather than the generic
  "kubernetes API error: Not Found".
- `get_pod` event-fetch failure handling: pod data is still returned with
  `events: []` and a logged warning, so the tool remains useful when the
  events endpoint is RBAC-restricted but pod read works.
- 16 tests for `get_pod` covering default/specific namespace, allowlist
  rejection, "all" rejection, 404 / 500 / unexpected exception, event field
  selector, sort + cap at 10, partial-success on event-fetch failure,
  container state shapes (running / waiting / terminated), init containers,
  defensive partial pod, event sort by `event_time` fallback, and input
  validation.

- `kube/safe.py`: `resolve_read_namespaces` resolver and `NamespaceNotAllowedError`
  exception. Centralises the `--namespaces` allowlist semantics defined in
  `docs/TOOLS_SPEC.md`: `None` → context default (rejected if not allowlisted,
  with a hint to specify a namespace or update `--namespaces`); `"all"` →
  sorted allowlist or full cluster; specific namespace → passes through unless
  outside the allowlist.
- `KubeContext.default_namespace`: read from the active context entry in
  kubeconfig at `load_context` time, falls back to `"default"`.
- `tools/pods.py`: `list_pods` tool with optional `namespace`, `label_selector`,
  `field_selector`, and `limit` (default 100, capped at 1000). Honours the
  allowlist via `resolve_read_namespaces`; iterates `list_namespaced_pod` per
  namespace when an allowlist is set, otherwise falls back to
  `list_pod_for_all_namespaces`. Output is sorted by `(namespace, name)` and
  includes a `truncated` flag.
- Pod formatter handles missing `metadata`/`spec`/`status` defensively
  (`Unknown`/`None`) so tests with partial mock objects don't crash.
- `patch_core_v1` factory fixture in `tests/conftest.py` for tool tests that
  need to mock the Core V1 API in their tool module. Existing `mock_core_v1`
  fixture remains in place.
- Tests: namespace allowlist resolver (`test_kube/test_safe.py`) and
  `list_pods` (specific / default / all-no-allowlist / all-with-allowlist /
  outside-allowlist / default-not-in-allowlist / selectors / truncation /
  sorting / format / no-containers / partial pod / API error / input
  validation).
- Project skeleton: `pyproject.toml` with pinned runtime deps (`mcp`, `kubernetes`,
  `pydantic`) and dev deps (`pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`,
  `mypy`); hatchling build backend; console script entry point.
- MIT `LICENSE` and Keep-a-Changelog formatted `CHANGELOG.md`.
- `src/k8s_mcp_server` package skeleton with version, CLI argument parser
  (`config.py`) supporting `--enable-writes`, `--namespaces`, `--kubeconfig`,
  `--context`, `--log-level`, `--version`.
- `kube/client.py`: kubeconfig loader and `KubeContext` dataclass. Honours
  `--kubeconfig` and `--context`, never echoes config contents.
- `tools/_registry.py`: `ToolResult` envelope and `@register_tool` decorator
  that captures name, description, pydantic input model, handler, and
  `is_write` flag.
- `utils/formatting.py`: `age_seconds_since` and `age_human` helpers
  (`5d`, `3h12m`, `45s`, …).
- `tools/namespaces.py`: `list_namespaces` tool. Respects the `--namespaces`
  allowlist (returns only allowlisted namespaces, `"all"` never bypasses it).
  Output is sorted by name for stable LLM consumption. K8s API call wrapped
  in `asyncio.to_thread` so the sync client doesn't block the event loop.
- `server.py`: MCP stdio server bootstrap. Builds the `Server`, registers
  `list_tools` / `call_tool` handlers, validates inputs through pydantic,
  filters out write tools when `--enable-writes` is False (defence-in-depth
  Layer 2 per `docs/SECURITY.md`).
- `__main__.py`: now actually runs the MCP stdio server via
  `asyncio.run(serve(settings))`. Catches `KubeConfigError` → exits 2 with a
  clear error message; catches `KeyboardInterrupt` → exits 0.
- GitHub Actions CI running `ruff format --check`, `ruff check`, `mypy --strict`,
  and `pytest` on Python 3.13.
- Tests: CLI parser, tool registry, formatting helpers, and `list_namespaces`
  (no allowlist / with allowlist / empty result / age fields / API exception /
  unexpected exception / extra-fields rejection / missing phase).
