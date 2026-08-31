"""Tests for experimental/postmaster_coverage.py -- the privacy-threshold
transition edge case. See that module's docstring for why it lives under
`experimental/`: no production caller ingests Postmaster domain-stats data
yet (CLOSE-1)."""

from datetime import date

import pytest

from deliverability_guard.engine.state import DataState
from deliverability_guard.experimental.postmaster_coverage import coverage_over_range
from deliverability_guard.signals.postmaster import DomainStatRow


def _row(day: date, value: float = 0.001, metric: str = "spam_rate") -> DomainStatRow:
    return DomainStatRow(metric_name=metric, day=day, value=value, source_day=day.isoformat())


def test_coverage_over_range_all_present_is_all_ok() -> None:
    rows = [_row(date(2026, 8, 1)), _row(date(2026, 8, 2)), _row(date(2026, 8, 3))]
    result = coverage_over_range(
        rows, metric_name="spam_rate", since=date(2026, 8, 1), until=date(2026, 8, 3)
    )
    assert [r.state for r in result] == [DataState.OK, DataState.OK, DataState.OK]
    assert all(not r.transition_alert for r in result)


def test_coverage_over_range_present_then_missing_is_stale_with_alert() -> None:
    """Domain drops below the privacy threshold after throttling ->
    transition alert, not a silent gap."""
    rows = [_row(date(2026, 8, 1))]  # no row for Aug 2 or Aug 3
    result = coverage_over_range(
        rows, metric_name="spam_rate", since=date(2026, 8, 1), until=date(2026, 8, 3)
    )
    assert [r.state for r in result] == [DataState.OK, DataState.STALE, DataState.INSUFFICIENT_DATA]
    assert [r.transition_alert for r in result] == [False, True, False]


def test_coverage_over_range_never_seen_data_is_insufficient_not_stale() -> None:
    result = coverage_over_range(
        [], metric_name="spam_rate", since=date(2026, 8, 1), until=date(2026, 8, 2)
    )
    assert [r.state for r in result] == [DataState.INSUFFICIENT_DATA, DataState.INSUFFICIENT_DATA]
    assert not any(r.transition_alert for r in result)


def test_coverage_over_range_recovery_is_ok_again() -> None:
    rows = [_row(date(2026, 8, 1)), _row(date(2026, 8, 3))]  # gap on Aug 2
    result = coverage_over_range(
        rows, metric_name="spam_rate", since=date(2026, 8, 1), until=date(2026, 8, 3)
    )
    assert [r.state for r in result] == [DataState.OK, DataState.STALE, DataState.OK]


def test_coverage_over_range_ignores_rows_for_a_different_metric() -> None:
    rows = [_row(date(2026, 8, 1), metric="auth_success_rate")]
    result = coverage_over_range(
        rows, metric_name="spam_rate", since=date(2026, 8, 1), until=date(2026, 8, 1)
    )
    assert result[0].state == DataState.INSUFFICIENT_DATA


def test_coverage_over_range_ignores_rows_with_no_day() -> None:
    rows = [DomainStatRow(metric_name="spam_rate", day=None, value=0.001, source_day=None)]
    result = coverage_over_range(
        rows, metric_name="spam_rate", since=date(2026, 8, 1), until=date(2026, 8, 1)
    )
    assert result[0].state == DataState.INSUFFICIENT_DATA


def test_coverage_over_range_rejects_since_after_until() -> None:
    with pytest.raises(ValueError, match="since"):
        coverage_over_range(
            [], metric_name="spam_rate", since=date(2026, 8, 3), until=date(2026, 8, 1)
        )


def test_coverage_over_range_single_day_range() -> None:
    result = coverage_over_range(
        [_row(date(2026, 8, 1))],
        metric_name="spam_rate",
        since=date(2026, 8, 1),
        until=date(2026, 8, 1),
    )
    assert len(result) == 1
    assert result[0].state == DataState.OK
