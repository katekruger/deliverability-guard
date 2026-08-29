"""Tests for engine/changepoint.py -- CUSUM sequential change detection."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deliverability_guard.engine.changepoint import CusumState, cusum_step


def test_zero_sends_leaves_statistic_unchanged() -> None:
    state = CusumState(cumulative=3.0)
    result = cusum_step(
        state, sends=0, complaints=0, target_rate=0.001, slack=0.0005, threshold=5.0
    )
    assert result.state.cumulative == 3.0
    assert result.alarmed is False


def test_steady_healthy_rate_does_not_alarm() -> None:
    state = CusumState()
    for _ in range(200):
        result = cusum_step(
            state, sends=500, complaints=0, target_rate=0.001, slack=0.0005, threshold=5.0
        )
        state = result.state
        assert result.alarmed is False


def test_sustained_shift_eventually_alarms() -> None:
    state = CusumState()
    alarmed_ever = False
    for _ in range(50):
        result = cusum_step(
            state, sends=500, complaints=10, target_rate=0.001, slack=0.0005, threshold=5.0
        )
        state = result.state
        if result.alarmed:
            alarmed_ever = True
            break
    assert alarmed_ever


def test_alarm_resets_the_statistic() -> None:
    state = CusumState(cumulative=100.0)
    result = cusum_step(state, sends=1, complaints=0, target_rate=0.0, slack=0.0, threshold=1.0)
    assert result.alarmed is True
    assert result.state.cumulative == 0.0


def test_cumulative_never_goes_negative() -> None:
    state = CusumState()
    result = cusum_step(
        state, sends=1000, complaints=0, target_rate=0.5, slack=0.0, threshold=1000.0
    )
    assert result.state.cumulative >= 0.0


@pytest.mark.parametrize(
    ("sends", "complaints", "target_rate", "slack", "threshold"),
    [
        (-1, 0, 0.001, 0.0005, 5.0),
        (5, -1, 0.001, 0.0005, 5.0),
        (5, 6, 0.001, 0.0005, 5.0),
        (5, 0, -0.1, 0.0005, 5.0),
        (5, 0, 1.1, 0.0005, 5.0),
        (5, 0, 0.001, -0.1, 5.0),
        (5, 0, 0.001, 0.0005, 0.0),
        (5, 0, 0.001, 0.0005, -1.0),
    ],
)
def test_invalid_arguments_raise(
    sends: int, complaints: int, target_rate: float, slack: float, threshold: float
) -> None:
    with pytest.raises(ValueError, match="must be"):
        cusum_step(
            CusumState(),
            sends=sends,
            complaints=complaints,
            target_rate=target_rate,
            slack=slack,
            threshold=threshold,
        )


@given(
    sends=st.integers(min_value=0, max_value=100_000),
    complaint_fraction=st.floats(min_value=0, max_value=1, allow_nan=False),
    cumulative=st.floats(min_value=0, max_value=1e6, allow_nan=False),
)
def test_cusum_step_never_crashes_and_stays_nonnegative(
    sends: int, complaint_fraction: float, cumulative: float
) -> None:
    complaints = int(sends * complaint_fraction)
    result = cusum_step(
        CusumState(cumulative=cumulative),
        sends=sends,
        complaints=complaints,
        target_rate=0.001,
        slack=0.0005,
        threshold=5.0,
    )
    assert result.state.cumulative >= 0.0
