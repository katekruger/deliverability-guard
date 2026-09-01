"""The two-loop daemon controller (BUILD-PLAN.md §5).

Runs the fast loop (poll provider stats, evaluate every mailbox, act via
the ladder) on a short cadence, and the slow loop (propose tighter
thresholds from the fast loop's own recent evidence) on a much longer
cadence, in ONE process, sharing one `ThresholdStore` and one
`BreakerStateStore`: a threshold the slow loop tightens takes effect on the
fast loop's very next tick, and a mailbox the ladder pauses stays paused
across every subsequent tick, exactly as it would across separate `check`
invocations (`cli.cmd_check` and this module's fast tick both call
`loops.fast.evaluate_all_mailboxes`, so the two paths cannot drift apart).

This is deliberately a POLLING fast loop, not a webhook receiver. Nothing
in this codebase yet accepts an inbound webhook -- that's real, separate
infrastructure (an HTTP server, per-provider signature verification) that
is out of scope here. Polling `driver.read_mailbox_stats` on a short
interval is a strictly weaker but honest substitute for "seconds-to-minutes
reaction to leading indicators" (BUILD-PLAN.md §5): the same data
`loops.fast.evaluate_signal` would eventually react to via a pushed webhook
is instead pulled on a timer.

The slow loop's evidence comes from the fast loop's own recent posterior
lower bounds -- a rolling window collected in-process -- not from Google
Postmaster or `signals.postmaster`. That keeps this controller usable with
zero additional setup (no OAuth) while still implementing the real
mechanism BUILD-PLAN.md §5 describes ("tunes fast-loop thresholds from
recent evidence"). A caller who has a live Postmaster/compliance signal can
wire it in via `compliance_degraded`, matching `loops.slow.tune_thresholds`'s
own decoupled contract -- this module doesn't import `signals.postmaster`
any more than `loops.slow` does.
"""

from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timedelta

from deliverability_guard.engine.breaker import (
    BreakerEvaluation,
    BreakerStateStore,
    ThresholdStore,
)
from deliverability_guard.engine.changepoint import CusumResult, CusumState
from deliverability_guard.engine.posterior import DEFAULT_MAX_POOLED_ESS, BetaDistribution
from deliverability_guard.loops.fast import (
    DEFAULT_CUSUM_SLACK,
    DEFAULT_CUSUM_TARGET_RATE,
    DEFAULT_CUSUM_THRESHOLD,
    evaluate_all_mailboxes,
)
from deliverability_guard.loops.slow import ThresholdAdjustment, tune_thresholds
from deliverability_guard.providers.base import MailboxRef, ProviderDriver

# How many recent posterior lower bounds the slow loop considers. Large
# enough to smooth over a single noisy tick, small enough that a real
# improvement (or regression) isn't drowned out by weeks-old evidence.
DEFAULT_LOWER_BOUND_WINDOW = 500


def run(
    *,
    driver: ProviderDriver,
    prior: BetaDistribution,
    dry_run: bool,
    state_store: BreakerStateStore,
    threshold_store: ThresholdStore,
    fast_interval: timedelta,
    slow_interval: timedelta,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    max_ticks: int | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    compliance_degraded: Callable[[], bool] = lambda: False,
    lower_bound_window: int = DEFAULT_LOWER_BOUND_WINDOW,
    max_pooled_ess: float = DEFAULT_MAX_POOLED_ESS,
    cusum_target_rate: float = DEFAULT_CUSUM_TARGET_RATE,
    cusum_slack: float = DEFAULT_CUSUM_SLACK,
    cusum_threshold: float = DEFAULT_CUSUM_THRESHOLD,
    on_fast_tick: Callable[[list[BreakerEvaluation]], None] | None = None,
    on_slow_tick: Callable[[ThresholdAdjustment], None] | None = None,
    on_cusum_alarm: Callable[[MailboxRef, CusumResult], None] | None = None,
    on_evaluation: Callable[[BreakerEvaluation], None] | None = None,
) -> None:
    """Run the two-loop controller.

    `now` and `sleep` are always injected -- this function never reads the
    real clock or calls `time.sleep` itself, so it's fully deterministic
    under test. `max_ticks=None` runs until `should_stop()` returns `True`
    (checked once per fast tick, before evaluating); a real daemon wires
    this to e.g. a signal handler setting a flag. `max_ticks` is primarily
    for tests and bounded demo runs -- it takes precedence when both it and
    `should_stop` would eventually stop the loop.

    Every fast tick evaluates every mailbox the driver reports on (via
    `loops.fast.evaluate_all_mailboxes`, against `threshold_store.current`
    snapshotted once at the start of that tick -- never re-read mid-tick,
    for the same "no torn read" reason `engine.breaker.evaluate` snapshots
    its own `thresholds` argument) and folds each evaluation's posterior
    lower bound (when there is one) into a rolling window. Once
    `slow_interval` has elapsed since the last slow tick, that window (plus
    `compliance_degraded()`) is handed to `loops.slow.tune_thresholds`; a
    proposed tighter ladder is applied to `threshold_store` immediately, so
    it's in effect for the very next fast tick.

    `cusum_states`, the CUSUM trend state per mailbox, is created once
    outside the loop and carried across every tick (mirroring
    `state_store`/`threshold_store`), so the running statistic actually
    accumulates evidence over the daemon's lifetime rather than resetting
    every tick -- see `loops.fast.evaluate_all_mailboxes`'s own docstring
    for why that matters.

    `on_evaluation`, when given, is passed straight through to
    `evaluate_all_mailboxes` and therefore called once per mailbox,
    immediately after that mailbox's own evaluation -- not after the whole
    tick's batch, the way `on_fast_tick` below is (CLOSE6-1). A caller that
    needs to durably persist each decision record before the next
    mailbox's provider action is attempted (e.g. `cli.cmd_run`) must use
    this, not `on_fast_tick`: if a later mailbox's evaluation in the same
    tick raises, `on_fast_tick` is never called for that tick at all, but
    `on_evaluation` has already run for every mailbox evaluated so far.
    """
    if max_ticks is not None and max_ticks <= 0:
        return

    recent_lower_bounds: deque[float] = deque(maxlen=lower_bound_window)
    cusum_states: dict[MailboxRef, CusumState] = {}
    last_slow_tick_at = now()
    tick = 0

    while True:
        if max_ticks is None and should_stop():
            return

        tick_time = now()
        since: date = tick_time.date() - timedelta(days=1)
        results = evaluate_all_mailboxes(
            driver=driver,
            since=since,
            prior=prior,
            thresholds=threshold_store.current,
            state_store=state_store,
            dry_run=dry_run,
            now=tick_time,
            max_pooled_ess=max_pooled_ess,
            cusum_states=cusum_states,
            cusum_target_rate=cusum_target_rate,
            cusum_slack=cusum_slack,
            cusum_threshold=cusum_threshold,
            on_cusum_alarm=on_cusum_alarm,
            on_evaluation=on_evaluation,
        )
        if on_fast_tick is not None:
            on_fast_tick(results)
        for result in results:
            if result.lower_bound is not None:
                recent_lower_bounds.append(result.lower_bound)

        if tick_time - last_slow_tick_at >= slow_interval:
            adjustment = tune_thresholds(
                recent_lower_bounds=list(recent_lower_bounds),
                current=threshold_store.current,
                compliance_degraded=compliance_degraded(),
            )
            if adjustment is not None:
                threshold_store.swap(adjustment.new_thresholds)
                if on_slow_tick is not None:
                    on_slow_tick(adjustment)
            last_slow_tick_at = tick_time

        tick += 1
        if max_ticks is not None and tick >= max_ticks:
            return
        sleep(fast_interval.total_seconds())
