"""Tests for experimental/warmup_advisor.py (BUILD-PLAN.md §4 item #19).

Warmup curves are folklore (BUILD-PLAN.md §8: no RFC, no M3AAWG document,
no independent research -- every source with numbers is a warmup vendor
selling warmup). These tests check the interpolation/tolerance MATH is
correct, not that the default curve's numbers are "right" -- there is no
ground truth to check them against, which is the whole point.

CLOSE3-5: moved from `identity/` to `experimental/` -- implemented and
tested, but `check_adherence` has no caller anywhere but this file. See the
module's own docstring."""

import pytest

from deliverability_guard.experimental.warmup_advisor import (
    DEFAULT_WARMUP_CURVE,
    AdherenceState,
    check_adherence,
    recommended_volume_for_day,
)

_SIMPLE_CURVE = ((1, 10), (11, 110))  # deliberately easy to interpolate by hand


def test_exact_checkpoint_day_returns_the_checkpoint_volume() -> None:
    assert recommended_volume_for_day(1, curve=_SIMPLE_CURVE) == 10
    assert recommended_volume_for_day(11, curve=_SIMPLE_CURVE) == 110


def test_interpolates_linearly_between_checkpoints() -> None:
    # Day 6 is halfway between day 1 (10) and day 11 (110): 10 + 0.5*100 = 60.
    assert recommended_volume_for_day(6, curve=_SIMPLE_CURVE) == 60


def test_day_before_the_first_checkpoint_uses_the_first_checkpoints_volume() -> None:
    curve = ((5, 20), (15, 120))
    assert recommended_volume_for_day(1, curve=curve) == 20
    assert recommended_volume_for_day(4, curve=curve) == 20


def test_day_past_the_last_checkpoint_returns_none() -> None:
    """Past the curve's range, the heuristic has nothing more to say -- a
    mailbox this far along is treated as fully warmed up, not "off
    schedule" against a curve that was never meant to extend this far."""
    assert recommended_volume_for_day(12, curve=_SIMPLE_CURVE) is None


def test_rejects_a_day_number_less_than_one() -> None:
    with pytest.raises(ValueError, match="day_number"):
        recommended_volume_for_day(0, curve=_SIMPLE_CURVE)


def test_rejects_an_empty_curve() -> None:
    with pytest.raises(ValueError, match="curve"):
        recommended_volume_for_day(1, curve=())


def test_rejects_a_curve_not_sorted_by_day() -> None:
    with pytest.raises(ValueError, match="sorted"):
        recommended_volume_for_day(1, curve=((5, 10), (1, 5)))


def test_the_default_curve_is_internally_valid() -> None:
    """The shipped default must itself satisfy the validation every custom
    curve is held to -- sorted, strictly increasing days, at least one
    point."""
    days = [day for day, _ in DEFAULT_WARMUP_CURVE]
    assert days == sorted(days)
    assert len(set(days)) == len(days)
    assert len(DEFAULT_WARMUP_CURVE) >= 1


# --- check_adherence -------------------------------------------------------


def test_actual_matching_the_recommendation_exactly_is_on_schedule() -> None:
    result = check_adherence(mailbox_day=6, actual_sends=60, curve=_SIMPLE_CURVE)
    assert result.state is AdherenceState.ON_SCHEDULE
    assert result.recommended_sends == 60


def test_actual_within_tolerance_is_on_schedule() -> None:
    # Default tolerance 0.25: 60 * 1.2 = 72, within +25%.
    result = check_adherence(mailbox_day=6, actual_sends=72, curve=_SIMPLE_CURVE, tolerance=0.25)
    assert result.state is AdherenceState.ON_SCHEDULE


def test_actual_meaningfully_above_tolerance_is_ahead_of_schedule() -> None:
    result = check_adherence(mailbox_day=6, actual_sends=200, curve=_SIMPLE_CURVE, tolerance=0.25)
    assert result.state is AdherenceState.AHEAD_OF_SCHEDULE


def test_actual_meaningfully_below_tolerance_is_behind_schedule() -> None:
    result = check_adherence(mailbox_day=6, actual_sends=10, curve=_SIMPLE_CURVE, tolerance=0.25)
    assert result.state is AdherenceState.BEHIND_SCHEDULE


def test_zero_recommended_volume_with_any_positive_sends_is_ahead() -> None:
    """A recommended volume of 0 (e.g. day 0-adjacent edge curves) makes a
    ratio-based tolerance check divide by zero -- any actual sends above 0
    must read as AHEAD_OF_SCHEDULE, not raise or silently read as on
    schedule."""
    curve = ((1, 0), (11, 100))
    result = check_adherence(mailbox_day=1, actual_sends=5, curve=curve)
    assert result.state is AdherenceState.AHEAD_OF_SCHEDULE


def test_zero_recommended_volume_with_zero_sends_is_on_schedule() -> None:
    curve = ((1, 0), (11, 100))
    result = check_adherence(mailbox_day=1, actual_sends=0, curve=curve)
    assert result.state is AdherenceState.ON_SCHEDULE


def test_day_past_the_curve_is_past_curve_regardless_of_actual_volume() -> None:
    result = check_adherence(mailbox_day=999, actual_sends=1, curve=_SIMPLE_CURVE)
    assert result.state is AdherenceState.PAST_CURVE
    assert result.recommended_sends is None


def test_rejects_negative_actual_sends() -> None:
    with pytest.raises(ValueError, match="actual_sends"):
        check_adherence(mailbox_day=1, actual_sends=-1, curve=_SIMPLE_CURVE)


def test_rejects_nonpositive_tolerance() -> None:
    with pytest.raises(ValueError, match="tolerance"):
        check_adherence(mailbox_day=1, actual_sends=10, curve=_SIMPLE_CURVE, tolerance=0.0)


def test_uses_the_default_curve_when_none_is_supplied() -> None:
    result = check_adherence(mailbox_day=1, actual_sends=10)
    assert result.recommended_sends == DEFAULT_WARMUP_CURVE[0][1]


def test_result_reports_the_inputs_it_was_given() -> None:
    result = check_adherence(mailbox_day=6, actual_sends=60, curve=_SIMPLE_CURVE, tolerance=0.3)
    assert result.mailbox_day == 6
    assert result.actual_sends == 60
    assert result.tolerance == 0.3
