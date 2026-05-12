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
  `--context`, `--log-level`, `--version`, and a stub entry point
  (`python -m k8s_mcp_server`) that parses args and logs startup.
- GitHub Actions CI running `ruff format --check`, `ruff check`, `mypy --strict`,
  and `pytest` on Python 3.13.
- Initial test suite covering CLI parser behaviour.
