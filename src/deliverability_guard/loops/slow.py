"""Slow loop: daily, tunes the fast loop's thresholds. Never trips the breaker.

The separation from the fast loop is enforced in types, not just by
convention: nothing in this module imports `ProviderDriver`,
`BreakerStateStore`, or anything else capable of pausing or throttling a
mailbox. `tune_thresholds` below receives plain numbers in and returns a
plain proposed `ThresholdLadder` out -- there is no object in scope through
which this module could call `pause()` even by a future mistake, because no
function here ever takes one as a parameter. (tests/test_slow_loop.py
enforces this at the source level too, as a second line of defense.)

Feeds on Postmaster data, compliance verdicts, and hierarchical pooling
(engine/posterior.py) -- BUILD-PLAN.md §5. Its job is to notice "the fast
loop's thresholds were too loose for the last three days" and propose
tighter ones; it is never the thing that decides to act on a single
mailbox.

Postmaster and hierarchical pooling feed in as plain `recent_lower_bounds`
-- the caller runs a domain's Postmaster `SPAM_RATE` (or a hierarchically-
pooled posterior from `engine.posterior.pooled_posterior`) through
`engine.posterior.update`/`lower_bound` the same way it would any other
rate, and passes the result here. This module doesn't know or care where a
lower bound came from, which is exactly what keeps it from needing to
import anything capability-bearing.

Compliance verdicts feed in as `compliance_degraded`, a plain bool -- NOT a
`DomainComplianceStatus` object from `signals.postmaster`. This is
deliberate: this module stays decoupled from that type specifically so it
never gains a reason to import anything from `signals` or `providers` at
all. `compliance_degraded` is for the unsubscribe-compliance verdicts
(`oneClickUnsubscribeVerdict` / `honorUnsubscribeVerdict` needing work) --
real risk signals worth tightening over, but not themselves evidence of an
active reputation emergency. `deliverabilityStatusVerdict` needing work is a
DIFFERENT signal entirely: it's the hard gate wired into
`engine.breaker.evaluate`'s `compliance_gate_tripped` parameter, which can
pause a mailbox outright -- see `signals.postmaster.forces_hard_gate`. That
path deliberately does NOT run through this module, because this module
must never be able to reach `pause()`.
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
    compliance_degraded: bool = False,
) -> ThresholdAdjustment | None:
    """Propose a tighter ladder if either is true:

    - Recent evidence (fast-loop posteriors, Postmaster rates, or
      hierarchically-pooled domain posteriors -- see module docstring) has
      been running close to `current.warn` without ever crossing it, the
      "your fast-loop thresholds were too loose for the last three days"
      case (BUILD-PLAN.md §5).
    - `compliance_degraded` is `True`: Google's unsubscribe-compliance
      verdicts need work, a real risk factor worth tightening over even
      with no elevated posterior evidence yet.

    Returns `None` if neither applies. This is a simple, explainable
    heuristic, not a claim of statistical sophistication: a legible reason
    a human reviewing the decision log can agree or disagree with matters
    more here than a more elaborate trend model would.
    """
    if not 0 < tighten_factor < 1:
        raise ValueError(f"tighten_factor must be strictly between 0 and 1, got {tighten_factor}")

    worst = max(recent_lower_bounds) if recent_lower_bounds else None
    evidence_close_to_warn = (
        worst is not None and current.warn * tighten_factor <= worst < current.warn
    )

    if not evidence_close_to_warn and not compliance_degraded:
        return None

    tightened = ThresholdLadder(
        warn=current.warn * tighten_factor,
        throttle=current.throttle * tighten_factor,
        pause=current.pause * tighten_factor,
    )
    reasons: list[str] = []
    if evidence_close_to_warn:
        assert worst is not None  # narrowed by evidence_close_to_warn above
        reasons.append(
            f"recent posterior lower bounds peaked at {worst:.4%}, within "
            f"{(1 - tighten_factor):.0%} of the warn threshold ({current.warn:.4%}) "
            f"without crossing it"
        )
    if compliance_degraded:
        reasons.append("Google's unsubscribe-compliance verdicts need work")
    return ThresholdAdjustment(
        new_thresholds=tightened,
        reason=f"{'; '.join(reasons)} -- tightening the ladder by {(1 - tighten_factor):.0%}",
    )
