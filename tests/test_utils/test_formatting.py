"""Tests for the age formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from k8s_mcp_server.utils.formatting import age_human, age_seconds_since


class TestAgeSecondsSince:
    def test_none_returns_zero(self) -> None:
        assert age_seconds_since(None) == 0

    def test_aware_timestamp(self) -> None:
        now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        ts = now - timedelta(seconds=300)
        assert age_seconds_since(ts, now=now) == 300

    def test_naive_timestamp_treated_as_utc(self) -> None:
        now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        ts = datetime(2026, 5, 13, 11, 59, 0)  # naive, 60s before now
        assert age_seconds_since(ts, now=now) == 60

    def test_future_timestamp_clamped_to_zero(self) -> None:
        now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        ts = now + timedelta(seconds=120)
        assert age_seconds_since(ts, now=now) == 0

    def test_non_utc_timezone(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        ts = datetime(2026, 5, 13, 6, 30, 0, tzinfo=eastern)  # 11:30 UTC
        assert age_seconds_since(ts, now=now) == 30 * 60


class TestAgeHuman:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0s"),
            (1, "1s"),
            (59, "59s"),
            (60, "1m"),
            (125, "2m5s"),
            (3599, "59m59s"),
            (3600, "1h"),
            (11520, "3h12m"),  # spec example
            (86399, "23h59m"),
            (86400, "1d"),
            (90000, "1d1h"),
            (432000, "5d"),  # spec example
            (365 * 86400, "1y"),
            (365 * 86400 + 86400, "1y1d"),
        ],
    )
    def test_format(self, seconds: int, expected: str) -> None:
        assert age_human(seconds) == expected
