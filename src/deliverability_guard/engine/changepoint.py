"""Sequential change detection (CUSUM) on the bounce/complaint stream.

Complaint data lags 24h-3 days behind send (BUILD-PLAN.md §5), so a breaker
reacting only to a fixed-window rate is reacting to a post-mortem. A one-sided
CUSUM statistic accumulates evidence of an upward shift in the complaint or
bounce rate day by day, and alarms as soon as the accumulated evidence
crosses a threshold -- which, for a real sustained shift, is reliably faster
than waiting for enough bad days to roll into a fixed window. Sequential
detection catches "something changed on Tuesday" without waiting for
Wednesday through Monday's window to confirm it.

CUSUM rather than SPRT: CUSUM's cumulative-deviation form is a more direct
fit for a running per-day count stream than SPRT's likelihood-ratio
formulation between two fixed hypotheses, and it's simpler to reason about
and test correctly for this use case (BUILD-PLAN.md §6 asks for "CUSUM or
SPRT" -- either is a valid sequential-detection answer to the fixed-window
problem).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CusumState:
    """Running CUSUM statistic. Pass the `state` from one `cusum_step` result
    into the next call -- this module holds no state of its own."""

    cumulative: float = 0.0


@dataclass(frozen=True, slots=True)
class CusumResult:
    state: CusumState
    alarmed: bool


def cusum_step(
    state: CusumState,
    sends: int,
    complaints: int,
    *,
    target_rate: float,
    slack: float,
    threshold: float,
) -> CusumResult:
    """One period's update to a one-sided CUSUM for an upward rate shift.

    `target_rate` is the rate this stream is expected to run at when
    healthy. `slack` (classically "k", the reference value) is how much
    drift above `target_rate` is tolerated per unit of volume before it
    counts as evidence of a shift -- too small and ordinary noise trips it,
    too large and it never fires. `threshold` (classically "h", the decision
    interval) is how much accumulated evidence is required before alarming.

    A period with `sends == 0` contributes no evidence either way and leaves
    the statistic unchanged: an outage or reporting gap should not itself
    look like either an improvement or a degradation. (Whether that gap is
    itself alert-worthy is engine/state.py's job, not this module's.)

    A period with `complaints > sends` is the same "no real evidence" case,
    not a distinct error (CLOSE7-2): bounce/complaint feedback lags sends by
    24h-3 days, so a real provider's aggregation window can legitimately
    report a complaint against a period whose matching sends haven't landed
    yet. `engine.breaker.evaluate` treats this identically -- see that
    function's own docstring -- and this is the same shared chokepoint
    (`loops.fast.evaluate_all_mailboxes`) both run against, so the two must
    agree on what counts as insufficient data.
    """
    if sends < 0:
        raise ValueError(f"sends must be >= 0, got {sends}")
    if complaints < 0:
        raise ValueError(f"complaints must be >= 0, got {complaints}")
    if not 0 <= target_rate <= 1:
        raise ValueError(f"target_rate must be in [0, 1], got {target_rate}")
    if slack < 0:
        raise ValueError(f"slack must be >= 0, got {slack}")
    if threshold <= 0:
        raise ValueError(f"threshold must be > 0, got {threshold}")

    if sends == 0 or complaints > sends:
        return CusumResult(state=state, alarmed=False)

    expected = (target_rate + slack) * sends
    deviation = complaints - expected
    new_cumulative = max(0.0, state.cumulative + deviation)
    alarmed = new_cumulative >= threshold
    new_state = CusumState(cumulative=0.0 if alarmed else new_cumulative)
    return CusumResult(state=new_state, alarmed=alarmed)
