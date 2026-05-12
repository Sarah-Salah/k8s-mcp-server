"""Tests for the CLI configuration parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from k8s_mcp_server.config import Settings, parse_args


def test_defaults() -> None:
    assert parse_args([]) == Settings(
        enable_writes=False,
        namespaces=None,
        kubeconfig=None,
        context=None,
        log_level="INFO",
    )


def test_enable_writes_flag() -> None:
    assert parse_args(["--enable-writes"]).enable_writes is True


def test_namespaces_parsed_to_tuple() -> None:
    assert parse_args(["--namespaces", "dev,staging"]).namespaces == ("dev", "staging")


def test_namespaces_strips_whitespace() -> None:
    assert parse_args(["--namespaces", " dev , staging "]).namespaces == ("dev", "staging")


def test_namespaces_empty_string_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--namespaces", " ,  "])


def test_kubeconfig_path() -> None:
    assert parse_args(["--kubeconfig", "/tmp/cfg"]).kubeconfig == Path("/tmp/cfg")


def test_context_override() -> None:
    assert parse_args(["--context", "prod"]).context == "prod"


def test_log_level_uppercased() -> None:
    assert parse_args(["--log-level", "debug"]).log_level == "DEBUG"


def test_log_level_invalid_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--log-level", "TRACE"])


def test_settings_is_frozen() -> None:
    settings = parse_args([])
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError on slotted dc
        settings.enable_writes = True  # type: ignore[misc]
