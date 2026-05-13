# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
