# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `tools/pods.py`: `delete_pod` write tool (registered with `is_write=True`)
  — the last tool in v1. Inputs: `name`, `namespace`, `force` (default
  `False`), `dry_run` (default `True`). Follows the Write Tool Contract
  from CLAUDE.md §6.1:
  - **Layer 3**: `assert_writes_enabled(settings)` as the first line —
    pinned by `test_delete_pod_writes_disabled_returns_layer3_error_before_any_api_call`.
  - **Rejects `namespace="all"`** upfront.
  - **Reads the pod first** to capture owner-reference info for audit;
    failed read → no audit (same asymmetry as `scale_deployment`).
  - **`force` and `dry_run` are independent flags.** All four combinations
    are valid and tested:
    - `force=False, dry_run=True` (default — safest): validate-only,
      K8s uses pod's `terminationGracePeriodSeconds`
    - `force=False, dry_run=False`: graceful delete with the pod's
      configured grace period
    - `force=True, dry_run=True`: validate an immediate-kill without
      applying
    - `force=True, dry_run=False`: actual immediate-kill — three opt-ins
      required (`--enable-writes` + `force=True` + `dry_run=False`)
  - **`force=True` → `grace_period_seconds=0`** on the K8s delete call
    (equivalent to `kubectl delete pod X --force --grace-period=0`).
    `force=False` omits the kwarg entirely so K8s uses the pod's
    `terminationGracePeriodSeconds` spec.
  - **`force` is SECURITY-CRITICAL in the audit log.** Pinned by
    `test_delete_pod_audit_log_includes_force_true_field_for_post_incident_grep`:
    the audit log line must include `force=True` so post-incident
    reviewers can grep for immediate-kill events without context.
  - **Owner-reference capture** via `_owner_controller_summary`: takes
    `metadata.owner_references[0]` (kind, name) — `None` for bare pods.
    Pods owned by a Deployment surface `controller_kind="ReplicaSet"` not
    `"Deployment"` because the pod's direct owner is the RS (the chain
    is Deployment → ReplicaSet → Pod). Pinned by
    `test_delete_pod_captures_replicaset_owner_for_deployment_pod` so
    future maintainers don't "fix" it.
  - **`propagation_policy` NOT passed** — pods own nothing that needs
    cascading deletion.
  - **404 race condition** between read and delete returns the same
    friendly `"pod 'X' not found in namespace 'Y'"` as 404-on-read.
- `docs/TOOLS_SPEC.md` tool #16 updated: replaced
  `grace_period_seconds: int = 30` with `force: bool = False` (binary
  opt-in is much more LLM-friendly than a numeric value with an
  arbitrary K8s default). Output expanded with `controller_kind`,
  `controller_name`, `force`. Added a note on the kubectl-equivalence
  of `force=True` and the audit-grep contract.
- 26 tests for `delete_pod` in `test_pods.py` covering the full Write
  Tool Contract: Layer 3 enforcement (no API call when writes disabled),
  namespace handling matrix, dry_run semantics + default True, **the
  full `force` matrix** (force=True→`grace_period_seconds=0`,
  force=False→kwarg absent, default=False, force+dry_run combinations
  both directions), **owner-reference capture across Deployment/
  StatefulSet/DaemonSet/bare-pod cases** (with the ReplicaSet pin
  serving as documentation), defensive missing owner_references and
  metadata, 404 on read + 404 on delete race + non-404 errors +
  unexpected exceptions on both calls, audit asymmetry (no audit on
  failed-read, audit on failed-delete), **the security-critical
  `force=True` grep test**, `is_write=True` registration sanity, and
  input validation.

- `tools/deployments.py`: `restart_deployment` write tool (registered with
  `is_write=True`). Inputs: `name`, `namespace`, `dry_run` (default `True`).
  Follows the Write Tool Contract from CLAUDE.md §6.1:
  - **Layer 3**: `assert_writes_enabled(settings)` as the first line —
    pinned by `test_restart_writes_disabled_returns_layer3_error_before_any_api_call`.
  - **Rejects `namespace="all"`** upfront.
  - **No read-before-patch** (unlike `scale_deployment`): restart has no
    "from" state worth capturing, so audit is always emitted before the
    single patch attempt and always present in `ToolResult.audit` (success
    or failure). No read-vs-patch asymmetry.
  - **Patches the main `Deployment` resource** (NOT the `/scale`
    sub-resource) with a deep-nested JSON-merge body setting
    `spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"]`.
    This mutates the pod template, which changes the template hash and
    triggers the deployment controller to spin up a new ReplicaSet —
    exactly what `kubectl rollout restart` does.
  - **Annotation key matches kubectl byte-for-byte** so external tools
    (Argo CD, Flux, observability dashboards) that parse rollout history
    find our restarts too.
  - **Timestamp generated ONCE per call** in RFC3339 format with `Z`
    suffix (e.g. `"2026-05-13T10:30:00Z"`) — the same Python string flows
    to the patch body, the audit log line, and the response. Pinned by
    `test_restart_timestamp_is_same_value_in_body_audit_and_response`
    (extracts the value from the patch call_args and asserts identity
    across all three sites).
  - **Format matches kubectl** byte-for-byte (Z-suffix, seconds
    precision) — pinned by `test_restart_timestamp_is_rfc3339_with_z_suffix`.
  - **Layer 4**: `dry_run="All"` forwarded when `dry_run=True`; omitted
    when `dry_run=False`; `applied` flag reflects.
  - **Layer 5**: `log_write_operation("restart_deployment", **audit)`
    emits before the patch attempt.
- `docs/TOOLS_SPEC.md` tool #15 output field renamed
  `restart_triggered_at` → `restarted_at` (aligning with the kubectl
  annotation key `restartedAt` and the audit-log keyval style). Added a
  note about the kubectl-interop rationale and timestamp lifecycle.
- 19 tests for `restart_deployment` in `test_deployments.py` covering
  the full Write Tool Contract: Layer 3 enforcement (no API call when
  writes disabled), namespace handling matrix, dry_run semantics + default
  True, **patch body deep-nesting and exact shape** including the canonical
  annotation key, **uses main deployment resource not /scale**,
  **no-read-before-patch**, **timestamp identity across body/audit/response**,
  **timestamp format matches kubectl RFC3339-Z**, audit envelope + log
  line presence, audit on failed patch (always — no asymmetry), 404
  friendly error, non-404 API error, unexpected exception, `is_write=True`
  registration sanity, and input validation.

- `tools/deployments.py`: `scale_deployment` write tool (registered with
  `is_write=True`). Inputs: `name`, `namespace`, `replicas` (`0–1000`),
  `dry_run` (default `True`). Follows the Write Tool Contract from
  CLAUDE.md §6.1 exactly:
  - **Layer 3**: `assert_writes_enabled(settings)` as the first line —
    no K8s call happens when `--enable-writes` is off, pinned by
    `test_scale_writes_disabled_returns_layer3_error_before_any_api_call`.
  - **Rejects `namespace="all"`** upfront.
  - **Reads the current deployment** for the audit envelope, then patches
    the `/scale` sub-resource (`patch_namespaced_deployment_scale`) with
    a JSON-merge body `{"spec": {"replicas": N}}`. The `/scale`
    sub-resource is canonical for replica updates and respects narrow
    RBAC (a user with only `update deployments/scale` can scale without
    full `update deployments`).
  - **Layer 4**: `dry_run="All"` is forwarded to the K8s API when the
    input `dry_run=True` (server-side validate-only). `dry_run=False`
    omits the kwarg entirely and marks `applied=True` in the response.
  - **Layer 5**: `log_write_operation("scale_deployment", ...)` emits
    BEFORE the patch attempt — failed patches still get audited.
  - **404 race condition** between read and patch returns the same
    friendly `"deployment 'X' not found in namespace 'Y'"` as 404-on-read.
  - **Audit asymmetry**: `ToolResult.audit` is populated on success AND
    on failed-PATCH paths (operation was attempted); NOT on failed-READ
    paths (operation wasn't attempted).
  - **`replicas_from=None` pass-through** when the source deployment has
    no `spec.replicas` set (rare/malformed) — consistent with
    `list_deployments`, no translation of K8s's None=1 convention.
- `docs/TOOLS_SPEC.md` tool #14 updated to match: field names
  `replicas_from`/`replicas_to` (was `previous_replicas`/`new_replicas`),
  `replicas` upper bound `1000` (was `100`). The spec stays the single
  source of truth and aligns with the audit-log keyval style.
- 24 tests for `scale_deployment` in `test_deployments.py` covering the
  full Write Tool Contract: Layer 3 enforcement (no K8s call when writes
  disabled), namespace handling (all/specific/default/allowlist), dry_run
  semantics (`"All"` kwarg presence/absence and `applied` flag), default
  `dry_run=True`, `/scale` sub-resource patch body, `replicas_from`
  capture from current deployment (including None pass-through), audit
  envelope + log line presence, audit asymmetry (failed-read=no audit /
  failed-patch=audit), 404-on-read and 404-on-patch race, non-404 read
  and patch errors, unexpected exceptions on both calls, boundary values
  (0 and 1000 accepted), input validation (negative, > 1000, missing
  required fields, extra fields), and a sanity check that the tool
  registers with `is_write=True` so Layer 2 filters it.

- `kube/safe.py`: `assert_writes_enabled(settings) -> ToolResult | None`
  — Layer 3 in-handler re-check of the `--enable-writes` flag. Returns a
  `ToolResult(success=False, error="write operations are disabled; restart
  the server with --enable-writes to enable")` when the flag is off, else
  `None`. Every write tool will call this as the first line of its handler
  body (see CLAUDE.md §6.1).
- `utils/audit.py`: `log_write_operation(tool_name, **fields)` — structured
  audit logger at INFO level on the logger `k8s_mcp_server.audit` (a
  stable name that is part of the public contract for operators). Format:
  `write_operation tool=<name> k1=v1 k2=v2 ...`. Applies field-name-based
  redaction (per `docs/SECURITY.md` regex: `token|secret|password|api[_-]?key|bearer`)
  before emit. Field-name-based, not entropy-based, by design — see the
  module docstring for the rationale.
- `tests/test_security.py`: cross-cutting tests for the write-tool
  infrastructure. Uses a dummy write tool registered via a snapshot+restore
  autouse fixture so the 13 real read tools don't interfere. Tests pin:
  Layer 2 filter (write tools excluded/included based on flag, read tools
  always visible, `visible == all - write_tools` set identity), Layer 3
  `assert_writes_enabled` (friendly error phrase pinned exactly, None on
  pass-through, full ToolResult envelope shape), and the audit logger
  (INFO level, stable `write_operation` prefix, keyval format, no-kwargs
  edge case, logger-name stability, password redaction, parametrized
  redaction across all SECURITY.md patterns incl. case-insensitive and
  `api[_-]?key` variants, non-redaction of unrelated fields like UUIDs).
- CLAUDE.md §6.1 "Write Tool Contract": documents the three layers of
  defense (flag / registry filter / in-handler `assert_writes_enabled`),
  the audit logger public-contract rule, the dry_run pattern, and the
  handler boilerplate every write tool must follow.

- `tools/metrics.py`: `top_nodes` tool. Inputs: `sort_by`
  (`Literal["cpu", "memory"]`, default `"cpu"`), `limit` (default 20,
  1–100 — smaller cap than `top_pods` since clusters rarely exceed 100
  nodes outside hyperscale). No `namespace` input — nodes are
  cluster-scoped; passing one is a `ValidationError`. Output per node:
  `{name, cpu_millicores, memory_mib, cpu_percent, memory_percent}`.
- `top_nodes` queries `metrics.k8s.io/v1beta1` via
  `CustomObjectsApi.list_cluster_custom_object(plural="nodes", ...)`.
  Reuses the existing `_cpu_to_millicores` / `_memory_to_mib` parsers
  and the metrics-server-missing 404 handler (`"metrics-server not
  available"` — pinned by
  `test_top_nodes_metrics_server_not_available_returns_friendly_error`).
- `top_nodes` percent calculation: a single batch
  `CoreV1Api.list_node()` call builds a `{name: {cpu_millicores,
  mem_mib}}` allocatable map. Percentages are `round()`-ed to int (matches
  `kubectl top nodes` display). **One extra API call total, not N** — the
  cost stays constant on 200-node clusters.
- `top_nodes` partial-success on capacity fetch failure: an `ApiException`
  or unexpected exception from `list_node` returns `cpu_percent: null` /
  `memory_percent: null` for every node (with a logged warning); usage
  values still surface. Per-field nullability: if a single node's
  allocatable is missing or has `allocatable.cpu="0"`, that node's
  percent fields are independently null while the other fields populate.
- **Overcommit (usage > allocatable) yields `percent > 100` and is
  surfaced as-is, NOT clamped at 100.** Commented in code: clamping
  would hide a real production signal (pods exceeding requests/limits,
  eviction risk) from the LLM.
- `top_nodes` sort: by `cpu_millicores` or `memory_mib` descending; ties
  broken by `name` ascending. Truncation: aggregate then cap at `limit`.
- 22 tests for `top_nodes` covering happy path with percent calc, int
  rounding, **overcommit-not-clamped**, partial-success matrix (alloc.cpu=0,
  alloc.memory unparseable, node missing from map, list_node fails with
  ApiException, list_node fails with unexpected exception, allocatable
  with status=None, V1Node with no metadata.name), metrics-server-missing
  friendly error, sort by cpu (default) and memory, name tiebreaker,
  truncation (over + under), empty items, items=None, defensive missing
  metadata/usage, non-404 API error (distinct from metrics-server-missing),
  unexpected exception, and input validation (namespace-field rejection +
  extra/sort_by/limit bounds).

- `tools/metrics.py`: `top_pods` tool. Inputs: `namespace`, `sort_by`
  (`Literal["cpu", "memory"]`, default `"cpu"`), `limit` (default 20, 1–200).
  Output per pod: `{name, namespace, cpu_millicores, memory_mib, containers:
  [{name, cpu_millicores, memory_mib}]}`. Pod-level numbers are sums of
  container numbers; per-container breakdown is preserved.
- `top_pods` queries the `metrics.k8s.io/v1beta1` API via
  `CustomObjectsApi.list_namespaced_custom_object` (or
  `list_cluster_custom_object` when `namespace="all"` and no allowlist).
  Honours the `--namespaces` allowlist (resolver-based; iterates per
  namespace when an allowlist is set).
- `top_pods` metrics-server detection: a `404` from the metrics API means
  the API itself isn't registered (a successful list on a cluster with no
  pods returns 200 + empty items). Returns the exact friendly error
  `"metrics-server not available"` — pinned by
  `test_metrics_server_not_available_returns_friendly_error`.
- `top_pods` Quantity parsing: CPU and memory strings are normalised via
  `kubernetes.utils.parse_quantity`. CPU values are converted to integer
  millicores (`int(Decimal cores * 1000)`); memory values to integer MiB
  (`int(Decimal bytes / 1048576)`). Sub-millicore CPU and sub-MiB memory
  truncate to 0 — intentional, matches `kubectl top pods` display
  semantics. Missing/unparseable Quantity strings → 0 so a malformed
  container doesn't error the whole pod.
- `top_pods` sort: by `cpu_millicores` or `memory_mib` descending; ties
  broken by `name` ascending.
- `top_pods` truncation: per-namespace aggregate then cap at `limit` with
  `truncated` flag — same pattern as `list_pods` / `list_events`.
- `patch_custom_objects` factory fixture in `tests/conftest.py`, parallel
  to the three existing `patch_*` factories. Used by `test_metrics.py`
  and any future tools that hit custom resources.
- ~26 tests for `top_pods` covering namespace dispatch (specific / None /
  all-no-allowlist / all-with-allowlist / outside-allowlist /
  default-not-in-allowlist), the **metrics-server-missing friendly error
  on both namespaced and cluster-wide paths**, CPU Quantity parsing
  (parametrized over nanocores / microcores / millicores / cores / 1m),
  memory Quantity parsing (parametrized over Gi / Mi / Ki / sub-MiB),
  defensive missing cpu / memory / usage / metadata / containers /
  empty items / None items, unparseable Quantity → 0, multi-container
  pod-level sum, sort by cpu (default) and memory, name tiebreaker,
  truncation (over + under), non-404 API error (distinct from
  metrics-server-missing), unexpected exception, and input validation
  (extra field, invalid sort_by Literal, invalid limit bounds).

- `tools/describe.py`: `describe_resource` tool. Polymorphic structured
  describe view across seven kinds (`pod`, `deployment`, `service`,
  `node`, `configmap`, `secret`, `ingress`). Output schema is consistent:
  `{kind, name, namespace, metadata, spec_summary, status, events}`.
  Kind is validated as a `Literal` in pydantic (invalid kinds rejected at
  schema parse time). Per-kind dispatch via a `_Describer` dataclass
  table — adding a new kind in v2 is a one-line table edit.
- `describe_resource` namespace handling: namespaced kinds use
  `resolve_read_namespaces` and reject `namespace="all"`; the
  cluster-scoped `node` kind rejects the `namespace` input entirely with
  a clear error.
- `describe_resource` event handling: separate `list_namespaced_event`
  call for event-generating namespaced kinds (pod / deployment / service
  / ingress), capped at the 5 most recent. Uses kind+name field selector
  (not UID — see project memory). Failure returns `events: []` with a
  warning logged. Skipped for node (cluster-scoped), configmap, and
  secret.
- `describe_resource` 404 → friendly per-kind error,
  e.g. `"pod 'X' not found in namespace 'Y'"` or `"node 'X' not found"`.
- **SECURITY-CRITICAL for `kind="secret"`:** `spec_summary` returns only
  `type` and `data_keys` (key names from `.data` ∪ `.stringData`); values
  are NEVER surfaced. Additionally, the
  `kubectl.kubernetes.io/last-applied-configuration` annotation is
  stripped from the response because it embeds the full applied JSON
  (including the base64-encoded `.data` block) for resources applied via
  `kubectl apply -f`. Two dedicated tests pin both protections:
  `test_describe_secret_redacts_data_values` and
  `test_describe_secret_strips_last_applied_configuration_annotation`.
  See `docs/SECURITY.md` "Sensitive Data Handling".
- `tools/conftest.py`: `patch_networking_v1` factory fixture for tools
  that use the networking.k8s.io/v1 API (currently only Ingress fetch).
  Parallel to `patch_core_v1` and `patch_apps_v1`.
- ~35 tests in `test_describe.py` covering: kind validation matrix
  (parametrized over all 7 valid + 6 invalid forms), namespace handling
  matrix (namespaced default / specific / all-rejected / outside-
  allowlist / default-not-in-allowlist / cluster-scoped namespace
  rejection / cluster-scoped no-namespace OK), per-kind happy paths for
  all 7 kinds, the two Secret security tests, Secret-no-data, non-Secret
  kinds keep the annotation, 404 namespaced + cluster-scoped, non-404 +
  unexpected exceptions, event field selector, 5-cap, partial-success on
  event failure (ApiException + unexpected), events skipped for
  node/configmap/secret, events fetched for ingress, event_time sort
  fallback, Service protocol TCP default, Ingress edge cases (missing
  backend.service, port=None, port with neither number nor name), Node
  Ready derivation matrix, defensive missing metadata/spec/status.

### Known duplication

- `_format_describe_event` (in `tools/describe.py`) is a slimmer variant of
  `_format_event` in `tools/pods.py` and `tools/events.py` — it omits the
  `involved_object` field (always redundant in describe because we filter
  events by kind+name upfront). The three near-duplicates will be
  consolidated in a follow-up refactor commit; the eventual shape (a
  tuple-returning helper or base+extension) is intentionally left open
  for that commit. Clearly commented in `describe.py`.
- `_ready_status_from_conditions` (in `tools/describe.py`) duplicates
  `_ready_status` in `tools/nodes.py`. Two callers is still within the
  rule of three; extraction is deferred to a future commit when a third
  tool needs Ready derivation. Clearly commented in `describe.py`.

- `tools/nodes.py`: `get_node` tool. Returns full node detail — name,
  derived status, roles (reusing `list_nodes`' logic), age, kubelet
  version, raw capacity/allocatable Quantity strings, full conditions
  list (via the shared `format_condition`), taints (`{key, value,
  effect}` — `time_added` dropped), and a `pods_on_node` count.
- `get_node` 404 → `"node 'X' not found"` (no namespace, since nodes are
  cluster-scoped).
- `get_node` pod count: separate `list_pod_for_all_namespaces` call with
  `field_selector="spec.nodeName=<name>"` and a `limit=1000` safety cap
  (kubelet default max pods/node is 110; production rarely exceeds 250).
  Failure (RBAC, API error) returns `pods_on_node: null` with a logged
  warning — same partial-success pattern as `get_pod`'s event fetch and
  `get_deployment`'s ReplicaSet fetch.
- `get_node` rejects a `namespace` input field (`ValidationError`), making
  the cluster-scoping explicit at the schema level.
- 15 tests for `get_node` covering happy path with full state, 404 / 500 /
  unexpected error, pod-count field selector + limit, partial-success on
  pod-count failure (ApiException + unexpected), conditions detail via
  shared helper, taints formatting (with-value / no-value / None →
  empty list), reuse of `_ready_status` and `_roles_from_labels` from
  `list_nodes`, defensive missing metadata/spec/status, namespace-field
  rejection, and input validation.

- `tools/nodes.py`: `list_nodes` tool. Inputs: `label_selector`, `limit`
  (default 100, 1–1000). No `namespace` input — nodes are cluster-scoped
  and the `--namespaces` allowlist does not apply (passing a `namespace`
  field is a `ValidationError`). Output per node: `{name, status, roles,
  age_seconds, age_human, kubelet_version, capacity, allocatable}`.
- `list_nodes` status derivation: iterates `status.conditions` for the
  `Ready` type. `status == "True"` → `"Ready"`,
  `status == "False"` → `"NotReady"`, anything else (`"Unknown"`, missing,
  or no `Ready` condition at all) → `"Unknown"`.
- `list_nodes` role derivation: matches labels by the
  `node-role.kubernetes.io/` prefix, takes the suffix, returns the sorted
  list. Bare prefix labels with empty suffix are skipped defensively. No
  role labels at all → `["worker"]` (matches `kubectl get nodes` display
  behaviour for unlabelled worker pools in GKE/EKS).
- `list_nodes` capacity/allocatable values are passed through as Quantity
  strings (`"4"`, `"8Gi"`, `"110"`) — no normalization. LLMs are trained
  on K8s resource strings and parsing them client-side would lose
  human-readable context. `kubelet_version` from `status.node_info`.
- Output sorted by `name`. Cluster-wide `limit` + truncation flag (same
  pattern as other list tools, just no per-namespace dispatch).
- 22 tests for `list_nodes` covering happy path / sort / age, label
  selector forwarding (set + None), truncation (over + under), the full
  status matrix (True / False / Unknown / no Ready condition / no
  conditions), role derivation (single / multiple-sorted / no-labels
  worker fallback / empty-suffix defensive), capacity & allocatable
  pass-through, defensive missing metadata/status, API exceptions, and
  input validation (incl. explicit rejection of `namespace` field as
  proof of cluster-scoping).

- `tools/services.py`: `list_services` tool. Inputs: `namespace`,
  `label_selector`, `limit` (default 100, 1–1000). Output per service:
  `{name, namespace, type, cluster_ip, external_ip, ports, age_seconds,
  age_human}`. `external_ip` is resolved from
  `status.load_balancer.ingress[0]` — `.ip` first, falling back to
  `.hostname` (AWS ELBs surface hostname; GCP surfaces ip); `None` for
  ClusterIP/NodePort/ExternalName and unprovisioned LoadBalancers.
- `list_services` port shape: `{name, port, target_port, protocol}` per
  entry, with `node_port` *only included when non-None* (NodePort and
  LoadBalancer services). `protocol` defaults to `"TCP"` when the K8s
  field is missing. `target_port` is passed through as-is (int or named
  port string).
- `list_services` namespace dispatch follows the established pattern:
  `list_namespaced_service` per namespace, or
  `list_service_for_all_namespaces` once when `namespace="all"` without
  an allowlist. Output sorted by `(namespace, name)`. Per-namespace
  `limit` + aggregate truncation flag.
- 22 tests for `list_services` covering namespace dispatch, label
  selector forwarding, truncation, sort order, the full `external_ip`
  resolution matrix (ClusterIP / LoadBalancer with ip / LoadBalancer
  with hostname / LoadBalancer with no ingress / LoadBalancer with
  empty-fields ingress entry), port formatting (protocol default,
  node_port omission, named target_port, multi-port preservation, empty
  list), defensive missing metadata/spec/status, API exceptions, and
  input validation.

- `tools/deployments.py`: `get_deployment` tool. Returns full deployment
  state — name, namespace, age, strategy (RollingUpdate / Recreate),
  `selector.match_labels`, all five replica counts
  (`replicas_desired`/`ready`/`available`/`updated`/`unavailable`), full
  container list with images, conditions (with
  `last_transition_age_seconds`), and the last 5 ReplicaSets as
  `rollout_history` (revision-sorted descending).
- `get_deployment` namespace handling: rejects `namespace="all"` upfront;
  otherwise defers to `resolve_read_namespaces`.
- `get_deployment` 404 → friendly
  `"deployment 'X' not found in namespace 'Y'"`.
- `get_deployment` rollout-history fetch: separate
  `list_namespaced_replica_set` call with `label_selector` built from
  `spec.selector.match_labels`. Returned ReplicaSets are filtered
  client-side to those whose `owner_references` include the deployment's
  UID AND `kind="Deployment"` (UID is canonical for owner_references —
  distinct from the kubelet-null-UID issue that prevents UID filtering on
  events). Sorted by the `deployment.kubernetes.io/revision` annotation
  parsed as int (missing/unparseable → `-1`, sorts to bottom). Capped at 5.
  `change_cause` surfaced from the `kubernetes.io/change-cause` annotation
  (often `None`).
- `get_deployment` partial-success handling: if `list_namespaced_replica_set`
  fails (RBAC, API error), the deployment data is still returned with
  `rollout_history: []` and a warning logged. Same pattern as `get_pod`'s
  event-fetch failure.
- 20 tests for `get_deployment` covering happy path, namespace allowlist
  matrix, 404 / 500 / unexpected error, label-selector construction,
  owner-UID + kind filtering, revision sort & 5-cap, missing-revision
  fallback, RS-fetch partial-success (ApiException + unexpected), empty
  `match_labels` skipping the RS call, full container list, status-replicas
  None→0 coercion, defensive missing metadata/spec/status, and input
  validation.

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

- Extracted shared `format_condition` helper to `utils/k8s_conditions.py`;
  `tools/pods.py` and `tools/deployments.py` now import from there. No
  behavior change. (Resolves the duplication introduced when `get_deployment`
  landed; promoted on the third condition-using tool surface per the rule
  of three.)
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
