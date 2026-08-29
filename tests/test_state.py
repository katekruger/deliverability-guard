"""Tests for engine/state.py -- the OK | INSUFFICIENT_DATA | STALE state machine."""

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from deliverability_guard.engine.state import (
    DailyReport,
    DataState,
    classify,
    evaluate_stream,
)

_DAY0 = date(2026, 1, 1)


def _day(n: int) -> date:
    return _DAY0 + timedelta(days=n)


def test_classify_ok_when_data_present() -> None:
    report = DailyReport(day=_day(0), sends=50, complaints=1)
    assert classify(report) == DataState.OK


def test_classify_insufficient_data_when_row_absent() -> None:
    report = DailyReport(day=_day(0), sends=None, complaints=None)
    assert classify(report) == DataState.INSUFFICIENT_DATA


def test_classify_insufficient_data_when_zero_sends() -> None:
    report = DailyReport(day=_day(0), sends=0, complaints=0)
    assert classify(report) == DataState.INSUFFICIENT_DATA


def test_evaluate_stream_present_then_absent_is_stale_with_alert() -> None:
    reports = [
        DailyReport(day=_day(0), sends=50, complaints=0),
        DailyReport(day=_day(1), sends=None, complaints=None),
    ]
    evaluations = evaluate_stream(reports)
    assert evaluations[0].state == DataState.OK
    assert evaluations[0].transition_alert is False
    assert evaluations[1].state == DataState.STALE
    assert evaluations[1].transition_alert is True


def test_evaluate_stream_alert_fires_only_on_the_transition_day() -> None:
    reports = [
        DailyReport(day=_day(0), sends=50, complaints=0),
        DailyReport(day=_day(1), sends=None, complaints=None),
        DailyReport(day=_day(2), sends=None, complaints=None),
    ]
    evaluations = evaluate_stream(reports)
    assert [e.state for e in evaluations] == [
        DataState.OK,
        DataState.STALE,
        DataState.INSUFFICIENT_DATA,
    ]
    assert [e.transition_alert for e in evaluations] == [False, True, False]


def test_evaluate_stream_never_seen_data_is_insufficient_not_stale() -> None:
    reports = [DailyReport(day=_day(0), sends=None, complaints=None)]
    evaluations = evaluate_stream(reports)
    assert evaluations[0].state == DataState.INSUFFICIENT_DATA
    assert evaluations[0].transition_alert is False


def test_evaluate_stream_recovery_is_ok_again() -> None:
    reports = [
        DailyReport(day=_day(0), sends=50, complaints=0),
        DailyReport(day=_day(1), sends=None, complaints=None),
        DailyReport(day=_day(2), sends=50, complaints=0),
    ]
    evaluations = evaluate_stream(reports)
    assert [e.state for e in evaluations] == [DataState.OK, DataState.STALE, DataState.OK]


def test_evaluate_stream_empty_input_returns_empty() -> None:
    assert evaluate_stream([]) == []


@st.composite
def _valid_or_absent_row(draw: st.DrawFn) -> tuple[int, int] | None:
    if draw(st.booleans()):
        return None
    sends = draw(st.integers(min_value=1, max_value=10_000))
    complaints = draw(st.integers(min_value=0, max_value=sends))
    return (sends, complaints)


@given(st.lists(_valid_or_absent_row(), min_size=1, max_size=30))
def test_evaluate_stream_never_reports_ok_without_data(
    rows: list[tuple[int, int] | None],
) -> None:
    """No matter what sequence of present/absent days arrives, a day with no
    sends is never classified OK -- the property this whole module exists to
    guarantee."""
    reports = [
        DailyReport(day=_day(i), sends=None, complaints=None)
        if row is None
        else DailyReport(day=_day(i), sends=row[0], complaints=row[1])
        for i, row in enumerate(rows)
    ]
    evaluations = evaluate_stream(reports)
    for report, evaluation in zip(reports, evaluations, strict=True):
        if not report.has_data:
            assert evaluation.state != DataState.OK
