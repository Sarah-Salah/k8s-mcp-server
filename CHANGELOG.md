# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `tools/deployments.py`: `list_deployments` tool. Inputs: `namespace`,
  `label_selector`, `limit` (default 100, 1–1000). Output per deployment:
  `{name, namespace, replicas_desired, replicas_ready, age_seconds,
  age_human, image}`. `replicas_desired` passes through `None` from
  `spec.replicas` (no translation of K8s's "None means 1" convention);
  `replicas_ready` coerces `None` → `0` since it's a count, not a config
  value. `image` is the first container's image
  (`spec.template.spec.containers[0].image`) — full container list lands in
  `get_deployment` (#6).
- `list_deployments` namespace dispatch follows the same pattern as
  `list_pods` / `list_events`: per-namespace `list_namespaced_deployment`
  calls when an allowlist is set or a single namespace is requested,
  `list_deployment_for_all_namespaces` once when `namespace="all"` and no
  allowlist. Output sorted by `(namespace, name)`. Per-namespace `limit` +
  aggregate truncation flag.
- `patch_apps_v1` factory fixture in `tests/conftest.py`, parallel to
  `patch_core_v1`. Used by `test_deployments.py` and any future apps/v1
  tools (StatefulSets / DaemonSets in v2). Existing `patch_core_v1` and
  every test that uses it stay untouched.
- 19 tests for `list_deployments` covering namespace dispatch, label
  selector forwarding, truncation, sort order, format shape, the
  `replicas_desired=None` pass-through, the `replicas_ready=None`→0
  coercion, multi-container image selection, no-containers image fallback,
  defensive partial-deployment handling, API exceptions, and input
  validation.

### Changed

- Extracted shared `event_sort_key` helper to `utils/k8s_events.py`;
  `tools/pods.py` and `tools/events.py` now import from there. No behavior
  change. (Resolves the temporary duplication introduced when `list_events`
  landed.)

### Added

- `tools/events.py`: `list_events` tool. Inputs: `namespace`,
  `involved_object_kind`, `involved_object_name`, `type` (validated as
  `Literal["Normal", "Warning"]`), `since_seconds` (`ge=1`), `limit`
  (default 50, 1–1000). `kind` + `name` + `type` are joined into the K8s
  `field_selector` string (kind+name not UID, matching the policy used by
  `get_pod`'s embedded events). `since_seconds` is filtered client-side
  because the K8s events API does not accept a timestamp field selector.
- `list_events` namespace dispatch: when an allowlist is set, iterates
  `list_namespaced_event` per allowed namespace; without an allowlist,
  `namespace="all"` calls `list_event_for_all_namespaces` once. `None` and
  specific namespaces use `list_namespaced_event` once.
- `list_events` output: `{events: [...], truncated: bool}`. Each event:
  `{type, reason, message, count, first_seen_age_seconds,
  last_seen_age_seconds, involved_object: {kind, name, namespace}}`. Sorted
  most-recent first using the same timestamp precedence as `get_pod`'s
  embedded events (`last_timestamp` → `event_time` → `metadata.creation_timestamp`
  → epoch fallback). Per-namespace `limit` + aggregate truncation matches
  `list_pods`.
- 22 tests for `list_events` covering namespace dispatch (default / specific /
  all-no-allowlist / all-with-allowlist / outside-allowlist / default-not-in-
  allowlist), field selector construction (full / none / kind-only),
  `since_seconds` filtering (including malformed events filtered out),
  sorting, truncation, format shape, `event_time` fallback, no-timestamps
  fallback, missing `involved_object`, API errors, and input validation
  (extra field / invalid `type` literal / case-sensitive `type` /
  out-of-range `since_seconds` and `limit`).

- `tools/logs.py`: `get_pod_logs` tool. Inputs: `name` (required), `namespace`,
  `container`, `tail_lines` (default 200, 1–10000), `since_seconds`,
  `previous` (default False), `max_bytes` (default 256 KiB, 1 KiB – 1 MiB).
  `tail_lines` and `since_seconds` are forwarded to the K8s API directly so
  the cluster does the filtering, not us.
- `get_pod_logs` namespace handling: rejects `namespace="all"` upfront;
  otherwise defers to `resolve_read_namespaces` (same allowlist semantics as
  `get_pod`).
- `get_pod_logs` container resolution: when `container` is omitted, pre-flights
  `read_namespaced_pod` to enumerate containers. Auto-picks the sole regular
  container if there is only one; otherwise returns an error listing every
  container name and pointing at the `container` parameter. Ephemeral
  containers (from `kubectl debug`) are intentionally not auto-resolved in v1
  — fetchable by passing `container=<name>` explicitly. Documented as a known
  limitation in the tool's docstring.
- `get_pod_logs` byte cap: response is trimmed from the start (most recent
  kept) when the encoded UTF-8 length exceeds `max_bytes`; a partial first
  line is dropped so output starts cleanly. `truncated=True` is set only when
  the byte cap fires — `tail_lines` / `since_seconds` are user-requested
  filters and do not flip the flag.
- `get_pod_logs` friendly error formatting:
  - `404` → `pod 'X' not found in namespace 'Y'`
  - `400 + previous=True` → `no previous logs for pod 'X' container 'Y':
    the container has not been restarted, or no previous instance exists`
  - other statuses → `kubernetes API error: <reason>`
- `get_pod_logs` logging: only metadata (pod, namespace, container, byte
  count, truncated, previous) is logged. Raw log content is never passed to
  any logger to avoid leaking PII, credentials in stack traces, internal
  URLs, or DB connection strings.
- 24 tests for `get_pod_logs` covering the namespace allowlist matrix, the
  pre-flight container resolver (auto-pick / multi-container error / no
  containers / 404 / 500 / missing spec / nameless container), K8s param
  forwarding, all three friendly-error branches, truncation
  (under cap / over cap with newlines / over cap with no newlines / empty /
  None from API), and input validation.

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
