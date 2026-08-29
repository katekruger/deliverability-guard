"""Slow loop: daily, tunes the fast loop's thresholds. Never trips the breaker.

The separation from the fast loop is enforced in types, not just by
convention: nothing in this module imports `ProviderDriver`,
`BreakerStateStore`, or anything else capable of pausing or throttling a
mailbox. `tune_thresholds` below receives plain numbers in and returns a
plain proposed `ThresholdLadder` out -- there is no object in scope through
which this module could call `pause()` even by a future mistake, because no
function here ever takes one as a parameter. (tests/test_slow_loop.py
enforces this at the source level too, as a second line of defense.)

Feeds on Postmaster data, compliance verdicts (Prompt 4), and hierarchical
pooling (engine/posterior.py) -- BUILD-PLAN.md §5. Its job is to notice "the
fast loop's thresholds were too loose for the last three days" and propose
tighter ones; it is never the thing that decides to act on a single mailbox.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from deliverability_guard.engine.breaker import ThresholdLadder

# How much to tighten by when recent evidence has been running close to the
# warn threshold without crossing it. 0.9 means "10% tighter."
_DEFAULT_TIGHTEN_FACTOR = 0.9


@dataclass(frozen=True, slots=True)
class ThresholdAdjustment:
    """A proposed new ladder, plus why. Applying it (or not) is the
    caller's decision -- this module only proposes."""

    new_thresholds: ThresholdLadder
    reason: str


def tune_thresholds(
    *,
    recent_lower_bounds: Sequence[float],
    current: ThresholdLadder,
    tighten_factor: float = _DEFAULT_TIGHTEN_FACTOR,
) -> ThresholdAdjustment | None:
    """Propose a tighter ladder if recent evidence has been running close
    to `current.warn` without ever crossing it -- exactly the "your
    fast-loop thresholds were too loose for the last three days" case
    (BUILD-PLAN.md §5). Returns `None` if no adjustment is warranted.

    This is a simple, explainable heuristic, not a claim of statistical
    sophistication: "the worst recent evidence got close to the line
    without crossing it" is a legible reason a human reviewing the decision
    log can agree or disagree with, which matters more here than a more
    elaborate trend model would.
    """
    if not 0 < tighten_factor < 1:
        raise ValueError(f"tighten_factor must be strictly between 0 and 1, got {tighten_factor}")
    if not recent_lower_bounds:
        return None

    worst = max(recent_lower_bounds)
    if current.warn * tighten_factor <= worst < current.warn:
        tightened = ThresholdLadder(
            warn=worst * tighten_factor,
            throttle=current.throttle * tighten_factor,
            pause=current.pause * tighten_factor,
        )
        return ThresholdAdjustment(
            new_thresholds=tightened,
            reason=(
                f"recent posterior lower bounds peaked at {worst:.4%}, within "
                f"{(1 - tighten_factor):.0%} of the warn threshold "
                f"({current.warn:.4%}) without crossing it -- tightening the "
                f"ladder by {(1 - tighten_factor):.0%}"
            ),
        )
    return None
