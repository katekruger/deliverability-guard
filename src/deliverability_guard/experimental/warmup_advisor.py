"""Warmup curve adherence advisor (BUILD-PLAN.md §4 item #19).

THIS IS FOLKLORE, LABELED AS SUCH (BUILD-PLAN.md §8): there is no RFC, no
M3AAWG document, and no independent research behind mailbox warmup ramp
curves. Every published curve with actual numbers comes from a warmup
vendor selling warmup as a product. `DEFAULT_WARMUP_CURVE` below is ONE
such curve -- vendor consensus, not a standard, not independently
verified -- and every function here accepts a `curve` override so a
caller is never stuck with it. BUILD-PLAN.md's own words: "Presenting
folklore as authoritative is the project's most likely credibility
failure." This module exists to not commit that failure.

BE HONEST ABOUT WHAT THIS IS, the same way `identity/subdomain_advisor.py`
is honest about its own limits: this module cannot and does not enforce
warmup. It compares a mailbox's actual observed daily send volume against
a recommended ramp curve and reports how far actual volume has departed
from it -- ahead of schedule (sending faster than the curve suggests) or
behind (slower). Whether either is actually harmful to THIS mailbox's real
reputation is not something a folklore curve can know; this is a
heuristic comparison against a baseline with no ground truth behind it,
not a verdict, and never presented as one.

STATUS (CLOSE3-5): moved here from `identity/`. `check_adherence` was fully
implemented, with 11 tests, but had no caller anywhere except its own test
file -- three audit rounds noted the same finding. Quarantined here rather
than deleted: this is real, correct, honestly-labeled logic, just not
wired into anything yet. README's "What this cannot see" section
correctly still lists warmup adherence as not implemented in the shipped
CLI -- promote this back to `identity/` and wire it (and update that
README line) once something calls it.
"""

from dataclasses import dataclass
from enum import Enum, auto
from itertools import pairwise

# (day_number, recommended_daily_volume) checkpoints, day 1 being the
# mailbox's first day of sending. Vendor-consensus numbers (BUILD-PLAN.md
# §8) -- NOT a standard, NOT independently verified. This particular shape
# (start low, roughly double every several days, plateau around week 5-6)
# is the commonly repeated pattern across warmup-vendor marketing and
# documentation, cited here as exactly that: common vendor guidance, not
# research. Override via `curve=` on every function below.
DEFAULT_WARMUP_CURVE: tuple[tuple[int, int], ...] = (
    (1, 10),
    (4, 20),
    (7, 30),
    (10, 50),
    (14, 80),
    (18, 120),
    (22, 170),
    (26, 230),
    (30, 300),
    (35, 400),
    (40, 500),
)

_DEFAULT_TOLERANCE = 0.25


class AdherenceState(Enum):
    ON_SCHEDULE = auto()
    AHEAD_OF_SCHEDULE = auto()
    BEHIND_SCHEDULE = auto()
    PAST_CURVE = auto()
    """`mailbox_day` is beyond the curve's last defined checkpoint. This is
    deliberately not a "bad" state: a mailbox this far along is treated as
    fully ramped, and the folklore curve has nothing more to recommend --
    see `recommended_volume_for_day`."""


@dataclass(frozen=True, slots=True)
class WarmupAdherence:
    mailbox_day: int
    actual_sends: int
    recommended_sends: int | None
    """`None` exactly when `state is PAST_CURVE` -- there is no
    recommendation to compare against past the curve's range."""
    state: AdherenceState
    tolerance: float


def recommended_volume_for_day(
    day_number: int, *, curve: tuple[tuple[int, int], ...] = DEFAULT_WARMUP_CURVE
) -> int | None:
    """The curve's recommended daily volume for `day_number`, piecewise-
    linearly interpolated between checkpoints.

    Before the first checkpoint's day, returns that checkpoint's volume
    (flat, not extrapolated below it -- a curve author chose where warmup
    "starts"). After the last checkpoint's day, returns `None`: the curve
    has nothing more to say, which `check_adherence` treats as "fully
    ramped," never as evidence of falling behind a schedule that was never
    defined that far out.
    """
    if day_number < 1:
        raise ValueError(f"day_number must be >= 1, got {day_number}")
    if not curve:
        raise ValueError("curve must not be empty")
    days = [day for day, _ in curve]
    if days != sorted(days) or len(set(days)) != len(days):
        raise ValueError(f"curve must be sorted by strictly increasing day, got {curve}")

    if day_number <= curve[0][0]:
        return curve[0][1]
    if day_number > curve[-1][0]:
        return None

    for (day_before, volume_before), (day_after, volume_after) in pairwise(curve):
        if day_before <= day_number <= day_after:
            span = day_after - day_before
            progress = (day_number - day_before) / span
            return round(volume_before + progress * (volume_after - volume_before))
    raise AssertionError("unreachable: day_number is within the curve's range by the checks above")


def check_adherence(
    *,
    mailbox_day: int,
    actual_sends: int,
    curve: tuple[tuple[int, int], ...] = DEFAULT_WARMUP_CURVE,
    tolerance: float = _DEFAULT_TOLERANCE,
) -> WarmupAdherence:
    """Compare `actual_sends` on `mailbox_day` against the curve's
    recommendation, classifying the result within `tolerance` (a fraction,
    e.g. 0.25 == +/-25%) as `ON_SCHEDULE`, or outside it as `AHEAD_OF_SCHEDULE`
    / `BEHIND_SCHEDULE`. `mailbox_day` past the curve's range is
    `PAST_CURVE` regardless of `actual_sends` -- see
    `recommended_volume_for_day`.
    """
    if actual_sends < 0:
        raise ValueError(f"actual_sends must be >= 0, got {actual_sends}")
    if tolerance <= 0:
        raise ValueError(f"tolerance must be > 0, got {tolerance}")

    recommended = recommended_volume_for_day(mailbox_day, curve=curve)
    if recommended is None:
        return WarmupAdherence(
            mailbox_day=mailbox_day,
            actual_sends=actual_sends,
            recommended_sends=None,
            state=AdherenceState.PAST_CURVE,
            tolerance=tolerance,
        )

    if recommended == 0:
        state = (
            AdherenceState.ON_SCHEDULE if actual_sends == 0 else AdherenceState.AHEAD_OF_SCHEDULE
        )
    else:
        ratio = actual_sends / recommended
        if ratio > 1 + tolerance:
            state = AdherenceState.AHEAD_OF_SCHEDULE
        elif ratio < 1 - tolerance:
            state = AdherenceState.BEHIND_SCHEDULE
        else:
            state = AdherenceState.ON_SCHEDULE

    return WarmupAdherence(
        mailbox_day=mailbox_day,
        actual_sends=actual_sends,
        recommended_sends=recommended,
        state=state,
        tolerance=tolerance,
    )
