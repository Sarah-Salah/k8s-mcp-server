# Tools Specification

This document defines every tool exposed by the MCP server. **Do not add tools that aren't in this document without first updating the spec.**

---

## Conventions

### Result envelope

Every tool returns a `ToolResult`:

```python
from pydantic import BaseModel
from typing import Any

class ToolResult(BaseModel):
    success: bool
    data: Any | None = None
    error: str | None = None
    audit: dict | None = None  # populated for write tools
```

### Input validation

Every tool has a paired pydantic input model named `<ToolName>Input`. Inputs are validated before the tool body runs. Invalid input returns `success=False, error="..."` — never raises.

### Namespace handling

If a tool takes a `namespace` parameter:
- `None` → use the kubeconfig context's default namespace
- `"all"` → all namespaces (read tools only; write tools reject this)
- Any other string → that specific namespace

If the server was started with `--namespaces ns1,ns2`, only those namespaces are valid.

### Output shape rules

- Trim verbose fields. We are returning data to an LLM, not a YAML viewer.
- Format timestamps as `age_seconds: int` plus `age_human: "3h12m"`.
- Limit collection responses by default (`limit: 100`) and indicate truncation with `truncated: True`.

---

## Read Tools

### 1. `list_namespaces`

**Description (for LLM):** List all namespaces in the cluster, with status and age.

**Input:** none

**Output:**
```json
{
  "namespaces": [
    {"name": "default", "status": "Active", "age_seconds": 432000, "age_human": "5d"}
  ]
}
```

---

### 2. `list_pods`

**Description:** List pods, optionally filtered by namespace, labels, or field selectors.

**Input:**
- `namespace: str | None` — namespace, `"all"`, or omit for context default
- `label_selector: str | None` — e.g. `"app=nginx,tier=frontend"`
- `field_selector: str | None` — e.g. `"status.phase=Running"`
- `limit: int = 100`

**Output:** list of `{name, namespace, phase, ready, restarts, age_seconds, age_human, node, pod_ip}`

---

### 3. `get_pod`

**Description:** Get a single pod's full state, including container statuses, conditions, and recent events.

**Input:**
- `name: str` (required)
- `namespace: str | None`

**Output:**
```json
{
  "name": "...",
  "namespace": "...",
  "phase": "Running",
  "conditions": [...],
  "containers": [
    {"name": "app", "image": "...", "ready": true, "restart_count": 0, "state": "running"}
  ],
  "events": [...]
}
```

---

### 4. `get_pod_logs`

**Description:** Get logs from a pod. Useful for debugging.

**Input:**
- `name: str` (required)
- `namespace: str | None`
- `container: str | None` — if pod has multiple containers
- `tail_lines: int = 200`
- `since_seconds: int | None` — e.g., 3600 for last hour
- `previous: bool = False` — get logs from previous instance (useful after a crash)

**Output:** `{logs: str, truncated: bool, container: str}`

---

### 5. `list_deployments`

**Description:** List deployments.

**Input:**
- `namespace: str | None`
- `label_selector: str | None`
- `limit: int = 100`

**Output:** list of `{name, namespace, replicas_desired, replicas_ready, age_seconds, age_human, image}`

---

### 6. `get_deployment`

**Description:** Get full deployment state with rollout history (last 5 revisions).

**Input:**
- `name: str`
- `namespace: str | None`

**Output:** detailed deployment info plus `replicasets: [...]` and `rollout_history: [...]`.

---

### 7. `list_services`

**Description:** List services.

**Input:** `namespace | label_selector | limit`

**Output:** list of `{name, namespace, type, cluster_ip, external_ip, ports, age_seconds}`

---

### 8. `list_nodes`

**Description:** List nodes with health and capacity.

**Input:** `label_selector | limit`

**Output:** list of `{name, status, roles, age_seconds, kubelet_version, capacity, allocatable}`

---

### 9. `get_node`

**Description:** Full node details including conditions and recent events.

**Input:** `name: str`

**Output:** node detail with conditions, taints, and pods-on-node count.

---

### 10. `list_events`

**Description:** Get cluster events, filtered and sorted by most recent.

**Input:**
- `namespace: str | None`
- `involved_object_kind: str | None` — e.g., `"Pod"`
- `involved_object_name: str | None`
- `type: str | None` — `"Normal"` or `"Warning"`
- `since_seconds: int | None`
- `limit: int = 50`

**Output:** list of `{type, reason, message, count, first_seen, last_seen, involved_object}`

---

### 11. `describe_resource`

**Description:** Generic `kubectl describe`-style output for any standard K8s resource.

**Input:**
- `kind: str` — `"pod" | "deployment" | "service" | "node" | "configmap" | "secret" | "ingress"`
- `name: str`
- `namespace: str | None` — required for namespaced kinds

**Output:** `{description: str}` — pre-formatted multi-section text.

**Important:** When `kind="secret"`, return only metadata — never the data block.

---

### 12. `top_pods`

**Description:** Pod resource usage (CPU, memory). Requires metrics-server installed in the cluster.

**Input:** `namespace | sort_by ("cpu" | "memory") | limit`

**Output:** list of `{name, namespace, cpu_millicores, memory_mib}`. If metrics-server is not installed, returns `success=False, error="metrics-server not available"`.

---

### 13. `top_nodes`

**Description:** Node resource usage. Same caveats as `top_pods`.

**Input:** `sort_by | limit`

**Output:** list of `{name, cpu_millicores, cpu_percent, memory_mib, memory_percent}`

---

## Write Tools

> All write tools:
> - Are registered **only** when the server was started with `--enable-writes`
> - Re-check the writes-enabled flag at call time (defense in depth)
> - Default `dry_run=True`
> - Reject `namespace="all"`
> - Emit an audit log line via `utils.audit`
> - Return an `audit` dict in the result

### 14. `scale_deployment`

**Description:** Set the replica count of a deployment.

**Input:**
- `name: str`
- `namespace: str | None`
- `replicas: int` — must be `>= 0` and `<= 100`
- `dry_run: bool = True`

**Output:**
```json
{
  "name": "...",
  "namespace": "...",
  "previous_replicas": 3,
  "new_replicas": 5,
  "dry_run": true,
  "applied": false
}
```

---

### 15. `restart_deployment`

**Description:** Trigger a rollout restart (equivalent to `kubectl rollout restart deployment/<name>`).

**Input:**
- `name: str`
- `namespace: str | None`
- `dry_run: bool = True`

**Output:** `{name, namespace, restart_triggered_at, dry_run, applied}`

---

### 16. `delete_pod`

**Description:** Delete a pod by name. Useful when a pod is stuck and you want it rescheduled.

**Input:**
- `name: str`
- `namespace: str | None`
- `grace_period_seconds: int = 30`
- `dry_run: bool = True`

**Output:** `{name, namespace, dry_run, applied}`

> Note: this only deletes the pod. If the pod is managed by a Deployment / StatefulSet / DaemonSet, a new one will be created automatically. The tool should mention this in its description.
