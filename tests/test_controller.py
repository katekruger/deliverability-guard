"""Tests for loops/controller.py: the two-loop daemon (BUILD-PLAN.md §5).

Runs the fast loop (poll provider stats, evaluate every mailbox) on a short
cadence and the slow loop (propose tighter thresholds from the fast loop's
own recent evidence) on a much longer cadence, in one process, sharing one
`ThresholdStore` and one `BreakerStateStore`. `now` and `sleep` are always
injected -- these tests never sleep or touch the real clock.
"""

from datetime import UTC, date, datetime, timedelta

from deliverability_guard.engine.breaker import (
    DEFAULT_LADDER,
    BreakerStateStore,
    ThresholdStore,
    Verdict,
)
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.loops.controller import run
from deliverability_guard.providers.base import MailboxDayStats, MailboxRef
from fixtures.fake_driver import FakeDriver

_START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_MAILBOX = MailboxRef(provider="fake", mailbox_id="a@example.com")


class _FakeClock:
    """A controllable clock: each call to `now()` advances by `step` and
    returns the new time. `sleep(seconds)` just advances the same clock by
    that many seconds instead of actually sleeping -- so a simulated day of
    daemon uptime costs nothing in real test time."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.sleep_calls: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current += timedelta(seconds=seconds)


def _healthy_driver() -> FakeDriver:
    return FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=_MAILBOX, day=date(2025, 12, 31), sends=5000, bounces=0)
        ]
    )


def test_run_stops_after_max_ticks() -> None:
    clock = _FakeClock(_START)
    driver = _healthy_driver()

    run(
        driver=driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(seconds=60),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=3,
    )

    # read_mailbox_stats is called once per fast tick.
    assert len(driver.read_calls) == 3


def test_run_sleeps_the_fast_interval_between_ticks() -> None:
    clock = _FakeClock(_START)
    driver = _healthy_driver()

    run(
        driver=driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(seconds=30),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=3,
    )

    # Sleeps between ticks, not after the last one: 3 ticks -> 2 sleeps.
    assert clock.sleep_calls == [30.0, 30.0]


def test_run_invokes_a_tick_callback_with_each_ticks_results() -> None:
    clock = _FakeClock(_START)
    driver = _healthy_driver()
    seen_verdicts: list[Verdict] = []

    run(
        driver=driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(seconds=60),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=1,
        on_fast_tick=lambda results: seen_verdicts.extend(r.verdict for r in results),
    )

    assert seen_verdicts == [Verdict.OK]


def test_run_shares_state_store_so_pause_is_idempotent_across_ticks() -> None:
    clock = _FakeClock(_START)
    breaching_driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=_MAILBOX, day=date(2025, 12, 31), sends=5000, bounces=40)
        ]
    )
    state_store = BreakerStateStore()

    run(
        driver=breaching_driver,
        prior=DEFAULT_PRIOR,
        dry_run=False,
        state_store=state_store,
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(seconds=60),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=5,
    )

    # Every tick re-evaluates as PAUSE, but only the first tick actually
    # calls pause() -- the rest are idempotent, exactly like repeated
    # `check` invocations against the same BreakerStateStore.
    assert breaching_driver.pause_calls == [_MAILBOX]


def test_run_tightens_thresholds_once_the_slow_interval_elapses() -> None:
    """Recent evidence sitting just under `warn` without crossing it, for
    long enough to cross the slow-loop interval, must tighten the shared
    ThresholdStore -- and the very next fast tick must evaluate against
    the tightened thresholds."""
    clock = _FakeClock(_START)
    # warn = 0.0005. Sends/complaints chosen so the posterior's lower bound
    # sits just under warn without crossing it.
    close_to_warn_driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=_MAILBOX, day=date(2025, 12, 31), sends=20_000, bounces=15)
        ]
    )
    threshold_store = ThresholdStore(DEFAULT_LADDER)

    run(
        driver=close_to_warn_driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=threshold_store,
        fast_interval=timedelta(hours=1),
        slow_interval=timedelta(hours=6),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=8,  # 8 hours of simulated uptime -> the slow loop must run
    )

    assert threshold_store.current != DEFAULT_LADDER
    assert threshold_store.current.warn < DEFAULT_LADDER.warn


def test_run_does_not_tighten_before_the_slow_interval_elapses() -> None:
    clock = _FakeClock(_START)
    close_to_warn_driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=_MAILBOX, day=date(2025, 12, 31), sends=20_000, bounces=15)
        ]
    )
    threshold_store = ThresholdStore(DEFAULT_LADDER)

    run(
        driver=close_to_warn_driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=threshold_store,
        fast_interval=timedelta(hours=1),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=3,  # far short of the 24h slow interval
    )

    assert threshold_store.current == DEFAULT_LADDER


def test_run_invokes_a_slow_tick_callback_on_adjustment() -> None:
    clock = _FakeClock(_START)
    close_to_warn_driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=_MAILBOX, day=date(2025, 12, 31), sends=20_000, bounces=15)
        ]
    )
    reasons: list[str] = []

    run(
        driver=close_to_warn_driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(hours=1),
        slow_interval=timedelta(hours=6),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=8,
        on_slow_tick=lambda adjustment: reasons.append(adjustment.reason),
    )

    assert len(reasons) == 1


def test_run_wires_compliance_degraded_into_the_slow_loop() -> None:
    """Even with no recent evidence at all, a `compliance_degraded` signal
    must still tighten the ladder once the slow interval elapses."""
    clock = _FakeClock(_START)
    no_stats_driver = FakeDriver(stats_to_return=[])
    threshold_store = ThresholdStore(DEFAULT_LADDER)

    run(
        driver=no_stats_driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=threshold_store,
        fast_interval=timedelta(hours=1),
        slow_interval=timedelta(hours=6),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=8,
        compliance_degraded=lambda: True,
    )

    assert threshold_store.current != DEFAULT_LADDER


def test_run_with_no_max_ticks_and_a_stop_signal_exits_cleanly() -> None:
    """`max_ticks=None` runs until `should_stop()` returns True -- the shape
    a real daemon uses (checking for e.g. a shutdown flag or signal), never
    exercised as a literal infinite loop in a test."""
    clock = _FakeClock(_START)
    driver = _healthy_driver()
    ticks = {"count": 0}

    def should_stop() -> bool:
        ticks["count"] += 1
        return ticks["count"] > 4

    run(
        driver=driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(seconds=1),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=None,
        should_stop=should_stop,
    )

    assert len(driver.read_calls) == 4


def test_run_skips_insufficient_data_evaluations_in_the_lower_bound_window() -> None:
    """A mailbox with zero sends produces `lower_bound=None`
    (`DataState.INSUFFICIENT_DATA`) -- it must not be folded into the slow
    loop's rolling window as if it were real evidence of a healthy rate."""
    clock = _FakeClock(_START)
    no_sends_driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=_MAILBOX, day=date(2025, 12, 31), sends=0, bounces=0)
        ]
    )
    seen_lower_bounds: list[float | None] = []

    run(
        driver=no_sends_driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(seconds=60),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=1,
        on_fast_tick=lambda results: seen_lower_bounds.extend(r.lower_bound for r in results),
    )

    assert seen_lower_bounds == [None]


def test_run_slow_tick_with_no_adjustment_needed_leaves_thresholds_unchanged() -> None:
    """A slow tick that genuinely runs (the interval elapsed) but finds
    nothing worth tightening must leave the shared ThresholdStore alone."""
    clock = _FakeClock(_START)
    driver = _healthy_driver()  # far below warn -- nothing to tighten
    threshold_store = ThresholdStore(DEFAULT_LADDER)

    run(
        driver=driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=threshold_store,
        fast_interval=timedelta(hours=1),
        slow_interval=timedelta(hours=6),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=8,  # long enough that the slow loop actually runs
    )

    assert threshold_store.current == DEFAULT_LADDER


def test_run_with_max_ticks_zero_does_nothing() -> None:
    clock = _FakeClock(_START)
    driver = _healthy_driver()

    run(
        driver=driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(DEFAULT_LADDER),
        fast_interval=timedelta(seconds=60),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=0,
    )

    assert driver.read_calls == []
    assert clock.sleep_calls == []


def test_run_default_thresholds_used_when_slow_loop_never_adjusts() -> None:
    clock = _FakeClock(_START)
    driver = _healthy_driver()
    threshold_store = ThresholdStore(DEFAULT_LADDER)

    run(
        driver=driver,
        prior=DEFAULT_PRIOR,
        dry_run=True,
        state_store=BreakerStateStore(),
        threshold_store=threshold_store,
        fast_interval=timedelta(seconds=60),
        slow_interval=timedelta(days=1),
        now=clock.now,
        sleep=clock.sleep,
        max_ticks=2,
    )

    assert threshold_store.current == DEFAULT_LADDER
