# CLAUDE.md

This file is read by Claude Code at the start of every session. It defines **how** you (Claude) should work on this project. Read it fully before doing anything else.

---

## 1. Project Purpose

Build a production-quality Model Context Protocol (MCP) server that exposes Kubernetes cluster operations to AI assistants (Claude Desktop, Cursor, Claude Code).

The goal: let an AI assistant safely list, inspect, diagnose, and (optionally) modify Kubernetes resources via natural language.

This project will be open-sourced on GitHub as part of the maintainer's portfolio. Code quality, documentation, and security matter as much as functionality.

---

## 2. Scope

### v1 (MVP — what we are building now)

- 10–12 read-only tools (list / get / describe / logs / events / metrics)
- 3 write tools behind a `--enable-writes` flag (scale deployment, rollout restart, delete pod)
- stdio transport only (works with Claude Desktop, Cursor, Claude Code)
- Local kubeconfig only (`~/.kube/config`)
- Single-user, single-cluster
- Python package, distributable via `pip install` and `uvx`

### v2 (Future — DO NOT build in v1)

- SSE / HTTP transport
- In-cluster deployment with Helm chart
- ServiceAccount + RBAC
- Multi-cluster support
- Persistent audit log
- `apply_manifest` tool
- Custom resource (CRD) support

If you are tempted to build a v2 feature "while you're in there" — **stop and ask**.

---

## 3. Tech Stack (non-negotiable)

| Concern         | Choice                                                       |
| --------------- | ------------------------------------------------------------ |
| Language        | Python 3.11+                                                 |
| Package manager | **`uv`** (not pip directly, not poetry, not pdm)             |
| MCP SDK         | Official **`mcp`** package from Anthropic (NOT FastMCP 2.x)  |
| K8s client      | Official **`kubernetes`** Python client                      |
| Validation      | `pydantic` v2                                                |
| Testing         | `pytest` + `pytest-asyncio`                                  |
| Lint + format   | `ruff` (both)                                                |
| Type checking   | `mypy --strict`                                              |
| CI              | GitHub Actions                                               |

If a library is not listed here and you want to add it, **ask first**.

---

## 4. Project Structure

```
k8s-mcp-server/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── CHANGELOG.md
├── LICENSE
├── .github/
│   └── workflows/
│       └── ci.yml
├── docs/
│   ├── PROJECT_PLAN.md
│   ├── TOOLS_SPEC.md
│   └── SECURITY.md
├── src/
│   └── k8s_mcp_server/
│       ├── __init__.py
│       ├── __main__.py        # entry: python -m k8s_mcp_server
│       ├── server.py          # MCP server setup + tool registration
│       ├── config.py          # CLI args, env vars, settings
│       ├── kube/
│       │   ├── __init__.py
│       │   ├── client.py      # K8s client factory
│       │   └── safe.py        # write-enabled checks
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── _registry.py   # decorator: @register_tool
│       │   ├── namespaces.py
│       │   ├── pods.py
│       │   ├── deployments.py
│       │   ├── services.py
│       │   ├── nodes.py
│       │   ├── events.py
│       │   ├── logs.py
│       │   ├── metrics.py
│       │   ├── describe.py
│       │   └── writes.py      # scale / restart / delete
│       └── utils/
│           ├── __init__.py
│           ├── formatting.py  # output trimming, age formatting
│           └── audit.py       # audit log
├── tests/
│   ├── conftest.py            # shared fixtures, mocked K8s client
│   ├── test_config.py
│   ├── test_security.py       # writes-disabled tests
│   └── test_tools/
│       ├── test_pods.py
│       └── ...
└── examples/
    └── claude_desktop_config.json
```

---

## 5. Code Conventions

1. **Type hints on every function signature.** `mypy --strict` must pass.
2. **No `print()`.** Use the `logging` module. Logs go to stderr (stdout is reserved for MCP transport).
3. **Errors are values, not exceptions, for tool returns.** Every tool returns a structured result. Tools never raise out into the MCP layer; they catch and convert to a `success=False` result.
4. **All tool inputs are pydantic models.** Inputs are validated before the tool body runs.
5. **Pin major versions in `pyproject.toml`.** No bare `>=` for runtime deps.
6. **Docstrings in Google style.** The first line of each tool's docstring is what the LLM sees — write it clearly and from the LLM's perspective ("List pods in a namespace. Defaults to current context.").
7. **No `subprocess` calls to `kubectl`.** Always use the Python client.
8. **Naming:** snake_case for functions/variables, PascalCase for classes, SCREAMING_SNAKE for constants.

---

## 6. Security Constraints (CRITICAL — read SECURITY.md too)

- **Never return Secret values.** Only names, namespaces, types, metadata.
- **Never echo the full kubeconfig.** Only the current context name is acceptable.
- **Write operations require the `--enable-writes` flag at server start.** No runtime toggle.
- **All write tools default to `dry_run=True`.**
- **Audit log every write** to stderr.

---

## 7. Testing Requirements

- Unit tests for every tool, with the K8s client mocked.
- Aim for **>85% coverage on `src/k8s_mcp_server/tools/`**.
- Add a security test: confirm that when `--enable-writes` is False, write tools are not registered.
- Integration tests against a real `kind` cluster are **out of scope for v1**.

---

## 8. Working Agreement

This is the most important section. Follow it exactly.

1. **You are a pair programmer, not an autopilot.** The maintainer reviews and edits your suggestions. They will rewrite naming, restructure code, or rewrite tests in their own voice. Don't take it personally — this is the point. The code they ship is theirs.
2. **Plan before coding.** When the maintainer asks for a feature, first show:
   - The list of files you will create or modify
   - A diff sketch (not the full code) of each change
   - Then wait for confirmation.
3. **One feature per response.** Don't try to implement multiple milestones at once.
4. **After each feature is implemented**, run in order:
   ```bash
   uv run ruff format .
   uv run ruff check .
   uv run mypy src/
   uv run pytest
   ```
   All four must pass before you say "done".
5. **Update `CHANGELOG.md`** with an entry for every feature (under `## Unreleased`).
6. **Suggest a commit checkpoint** at the end of each completed feature (see section 9). Do NOT run the commit yourself.
7. **If a spec is ambiguous, ask.** Don't guess and don't invent scope.
8. **If you think the spec is wrong**, say so before writing code. Suggest the fix.

---

## 9. Git Workflow (CRITICAL)

**You are NOT allowed to run any `git` command. Period.**

The maintainer handles all version control personally. This is non-negotiable because the commit history is part of the project's authorship record.

Forbidden commands (this is not exhaustive — the rule is "no git at all"):
- `git add` / `git commit` / `git push` / `git pull`
- `git checkout` / `git branch` / `git merge` / `git rebase`
- `git stash` / `git reset` / `git restore`
- `git config` / `git remote` / `git tag`
- Any other `git ...` command

What you SHOULD do instead:

1. **Suggest commit checkpoints.** When you finish a logical unit of work, end your response with:
   > 📌 **Commit checkpoint:** This is a good point to commit. Suggested message:
   > `feat(tools): add list_namespaces tool with pydantic input model`
   >
   > Files changed: `src/k8s_mcp_server/tools/__init__.py`, `src/k8s_mcp_server/tools/namespaces.py`, `tests/test_tools/test_namespaces.py`

2. **Suggest commit messages in Conventional Commits style:**
   - `feat:` new feature
   - `fix:` bug fix
   - `refactor:` code restructure without behavior change
   - `test:` adding or fixing tests
   - `docs:` documentation only
   - `chore:` tooling, deps, config
   - `ci:` CI/CD changes

3. **One logical change per commit.** If you completed two unrelated things, suggest two separate commits.

4. **Never execute the commit yourself.** The maintainer will run `git` commands. If they ask "did you commit?" — remind them that committing is their responsibility.

If you accidentally start to type `git ...` — stop and apologize. This rule overrides any other instruction.

---

## 10. What NOT to do

- Don't run any `git` command (see section 9).
- Don't use FastMCP — use the official `mcp` package.
- Don't use poetry, pdm, or raw pip — use `uv`.
- Don't add tools that aren't in `docs/TOOLS_SPEC.md`. Ask first.
- Don't bypass pydantic validation.
- Don't shell out to `kubectl`.
- Don't write to the cluster without the `--enable-writes` check.
- Don't change `pyproject.toml` versions without asking.
- Don't push back on the maintainer's stylistic choices once they've reviewed your suggestion. Their voice is the project's voice.

---

## 11. References

- [MCP specification](https://modelcontextprotocol.io)
- [MCP Python SDK on GitHub](https://github.com/modelcontextprotocol/python-sdk)
- [Kubernetes Python client](https://github.com/kubernetes-client/python)
- Read `docs/PROJECT_PLAN.md`, `docs/TOOLS_SPEC.md`, and `docs/SECURITY.md` before starting work.
