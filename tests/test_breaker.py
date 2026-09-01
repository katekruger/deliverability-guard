"""Tests for engine/breaker.py: the ladder, idempotent pause, and dry-run identity."""

import dataclasses
import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest

from deliverability_guard.audit.log import (
    DecisionRecord,
    ResumeRecord,
    append_record,
    append_resume_record,
    read_records,
)
from deliverability_guard.engine.breaker import (
    DEFAULT_LADDER,
    BreakerStateStore,
    BreakerStateStoreLoadError,
    MailboxBreakerStatus,
    ThresholdLadder,
    ThresholdStore,
    Verdict,
    evaluate,
    rung,
)
from deliverability_guard.engine.posterior import DEFAULT_PRIOR, GroupObservation
from deliverability_guard.engine.state import DataState
from deliverability_guard.providers.base import (
    ActionOutcome,
    Capability,
    MailboxRef,
    RateLimitExceededError,
)
from fixtures.fake_driver import FakeDriver

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_MAILBOX = MailboxRef(provider="fake", mailbox_id="a@example.com")


# --- rung() and ThresholdLadder -----------------------------------------


def test_rung_below_warn_is_ok() -> None:
    assert rung(0.0001, DEFAULT_LADDER) == Verdict.OK


def test_rung_at_warn_is_warn() -> None:
    assert rung(DEFAULT_LADDER.warn, DEFAULT_LADDER) == Verdict.WARN


def test_rung_at_throttle_is_throttle() -> None:
    assert rung(DEFAULT_LADDER.throttle, DEFAULT_LADDER) == Verdict.THROTTLE


def test_rung_at_pause_is_pause() -> None:
    assert rung(DEFAULT_LADDER.pause, DEFAULT_LADDER) == Verdict.PAUSE


def test_rung_above_pause_is_still_pause() -> None:
    assert rung(1.0, DEFAULT_LADDER) == Verdict.PAUSE


def test_threshold_ladder_rejects_out_of_order_values() -> None:
    with pytest.raises(ValueError, match="warn"):
        ThresholdLadder(warn=0.002, throttle=0.001, pause=0.003)


def test_threshold_store_swap_is_a_full_replacement() -> None:
    store = ThresholdStore(DEFAULT_LADDER)
    tighter = ThresholdLadder(warn=0.0001, throttle=0.0002, pause=0.0003)
    store.swap(tighter)
    assert store.current == tighter


# --- evaluate(): data state -----------------------------------------------


def test_zero_sends_is_insufficient_data_and_takes_no_action() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=0,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert result.verdict == Verdict.OK
    assert result.action is None
    assert driver.pause_calls == []
    assert driver.throttle_calls == []


def test_rejects_negative_sends() -> None:
    with pytest.raises(ValueError, match="sends"):
        evaluate(
            driver=FakeDriver(),
            mailbox=_MAILBOX,
            sends=-1,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=_NOW,
        )


def test_rejects_complaints_greater_than_sends() -> None:
    with pytest.raises(ValueError, match="complaints"):
        evaluate(
            driver=FakeDriver(),
            mailbox=_MAILBOX,
            sends=5,
            complaints=6,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=_NOW,
        )


# --- evaluate(): the ladder itself ----------------------------------------


def test_healthy_mailbox_is_ok_and_takes_no_action() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert result.verdict == Verdict.OK
    assert result.action is None


def test_warn_verdict_takes_no_provider_action() -> None:
    """Warn is notify-only -- the ladder never calls the provider for it."""
    driver = FakeDriver()
    # Handcrafted to land in the warn band: enough complaints that the
    # lower bound sits between warn and throttle.
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=20,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert result.verdict == Verdict.WARN
    assert result.action is None
    assert driver.pause_calls == []
    assert driver.throttle_calls == []


def test_throttle_verdict_reduces_daily_limit_by_half() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert result.verdict == Verdict.THROTTLE
    assert driver.throttle_calls == [("a@example.com", 50)]


def test_throttle_verdict_without_known_daily_limit_is_unsupported() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=None,
    )
    assert result.action is not None
    assert result.action.outcome == ActionOutcome.UNSUPPORTED
    assert driver.throttle_calls == []


def test_throttle_that_would_drop_below_the_floor_escalates_to_pause() -> None:
    """ENG-5a: a mailbox already down to a daily limit of 1 has nowhere left
    to throttle -- halving it again would floor-clamp to 1 forever, which is
    a pause wearing a different hat (see the module docstring). The correct
    response is to escalate to PAUSE, which goes through the human-review
    gate (ADR 0003), not to silently keep "throttling" at the same floor.

    This deliberately changes the old assertion
    (`driver.throttle_calls == [("a@example.com", 1)]`), which encoded the
    exact bug this fixes: a throttle floor-clamped forever, never reaching
    PAUSE and never passing through human review."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=1,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.throttle_calls == []
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- CLOSE3-2: an unexecutable throttle must not loop forever -------------


def test_unsupported_throttle_escalates_to_pause_after_a_bounded_streak() -> None:
    """CLOSE3-2: before this, a mailbox whose current daily limit is unknown
    (or whose provider structurally can't throttle) stayed ACTIVE forever,
    re-deriving THROTTLE and re-emitting an identical UNSUPPORTED record on
    every single evaluation. After a bounded number of consecutive
    unexecutable throttles, this must escalate to PAUSE -- through the
    human-review gate (ADR 0003), same as the floor-escalation case just
    above."""
    driver = FakeDriver(throttle_outcome=ActionOutcome.UNSUPPORTED)
    state_store = BreakerStateStore()
    results = [
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
        for _ in range(4)
    ]
    assert [r.verdict for r in results] == [
        Verdict.THROTTLE,
        Verdict.THROTTLE,
        Verdict.THROTTLE,
        Verdict.PAUSE,
    ]
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_a_successful_throttle_resets_the_unsupported_streak() -> None:
    """The streak is specific to consecutive unexecutable throttles -- a
    successful throttle in between must reset it, not let it silently
    accumulate toward an escalation the mailbox no longer deserves."""
    driver = FakeDriver(throttle_outcome=ActionOutcome.UNSUPPORTED)
    state_store = BreakerStateStore()
    for _ in range(2):  # below the escalation bound of 3
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
    driver.throttle_outcome = ActionOutcome.PERFORMED
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert result.verdict == Verdict.THROTTLE
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED


def test_state_store_from_log_bounds_repeated_unsupported_throttle_records(
    tmp_path: Path,
) -> None:
    """CLOSE3-2's own required test shape: ten separate restarts (a fresh
    `BreakerStateStore.from_log` each time), a driver that always reports
    UNSUPPORTED for throttle. The log must not accumulate ten identical
    UNSUPPORTED throttle records -- the streak persists across restarts via
    `from_log`, same as `_throttled_at_limit` (CLOSE3-1)."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(throttle_outcome=ActionOutcome.UNSUPPORTED)
    for _ in range(10):
        # CLOSE4-2: all ten iterations run, unconditionally -- unattended
        # re-running is exactly the documented deployment (`check` is "the
        # smallest thing you can put in cron," and nobody watches cron).
        # Breaking out early on PAUSED status used to be the only thing
        # standing between this test and CLOSE4-1's bug: once escalated to
        # PAUSE, a THROTTLE/UNSUPPORTED record replayed afterward used to
        # un-pause the mailbox, and the very next tick would throttle-escalate
        # and pause it AGAIN -- a real provider call every four runs, forever,
        # not the one call this test now proves happens.
        state_store = (
            BreakerStateStore.from_log(log_path) if log_path.exists() else BreakerStateStore()
        )
        result = evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
        append_record(log_path, DecisionRecord.from_evaluation(result))

    records = read_records(log_path)
    unsupported_throttle_records = [
        r
        for r in records
        if r.verdict is Verdict.THROTTLE and r.action_outcome is ActionOutcome.UNSUPPORTED
    ]
    assert len(unsupported_throttle_records) < 10
    assert BreakerStateStore.from_log(log_path).status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    # CLOSE4-1/CLOSE4-2: once escalated to PAUSE, a THROTTLE/UNSUPPORTED
    # record replayed afterward must never un-pause the mailbox -- so
    # exactly ONE real provider pause call happens across all ten runs,
    # not one every time the streak re-crosses the bound.
    assert driver.pause_calls == [_MAILBOX]


# --- Idempotency: THROTTLE, keyed on the verdict, not the limit ------------


def test_repeated_throttle_verdict_does_not_re_halve_the_daily_limit() -> None:
    """ENG-5a's reproduction: six identical THROTTLE evaluations against one
    mailbox must not compound (50 -> 25 -> 12 -> 6 -> 3 -> 1). Only the
    first evaluation actually calls throttle(); the rest are idempotent
    no-ops, exactly like repeated PAUSE."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    for _ in range(6):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
    assert driver.throttle_calls == [("a@example.com", 50)]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED


def test_failed_throttle_does_not_mark_the_mailbox_throttled() -> None:
    """If the provider call itself fails, the mailbox must not be marked
    THROTTLED -- that would make a genuinely failed throttle silently
    idempotent-no-op on every future retry."""
    driver = FakeDriver(throttle_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert driver.throttle_calls == [("a@example.com", 50)]


def test_throttle_idempotency_does_not_block_a_first_time_pause() -> None:
    """A mailbox already THROTTLED must still PAUSE normally once its
    evidence escalates to the pause rung -- idempotency is per-verdict, not
    a blanket "never act on this mailbox again"."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED

    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- CLOSE-3: throttle recovery, re-throttle, floor escalation off-by-one --


def test_throttle_then_ok_then_throttle_re_throttles() -> None:
    """CLOSE-3b: a sustained OK verdict clears THROTTLED back to ACTIVE --
    the ladder's own recovery path for THROTTLE, unlike PAUSE. A later
    re-degradation must reach the provider again, not be swallowed as an
    idempotent no-op against a status that never recovered."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED

    ok_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    assert ok_result.verdict == Verdict.OK
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE

    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    assert driver.throttle_calls == [("a@example.com", 50), ("a@example.com", 25)]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED


def test_throttle_re_throttles_when_the_current_limit_has_grown_past_what_was_applied() -> None:
    """CLOSE-3b/ENG-5a: the idempotency key is (verdict, applied limit), not
    just the status. A mailbox stays THROTTLED throughout here (no
    intervening OK), but its current limit is later reported HIGHER than
    what the breaker last applied -- evidence something restored it outside
    the breaker's own bookkeeping. That must reach the provider again, not
    be swallowed as idempotent."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,  # -> throttled to 50
    )
    assert driver.throttle_calls == [("a@example.com", 50)]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED

    # An identical re-evaluation with the SAME current_daily_limit (100) is
    # still idempotent, exactly like the six-identical-ticks case.
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert driver.throttle_calls == [("a@example.com", 50)]

    # Now the mailbox's real current limit is reported HIGHER than 100 --
    # something restored it beyond where the breaker last found it.
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=150,
    )
    assert driver.throttle_calls == [("a@example.com", 50), ("a@example.com", 75)]


def test_failed_pause_does_not_let_a_later_throttle_re_halve() -> None:
    """CLOSE-3c's reproduction: THROTTLE -> 25; a PAUSE attempt in between
    FAILS; the next THROTTLE (against the same, unchanged current limit)
    must stay idempotent, not halve again (25 -> 12)."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,  # -> throttled to 25
    )
    assert driver.throttle_calls == [("a@example.com", 25)]

    driver.pause_outcome = ActionOutcome.FAILED
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=25,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_FAILED

    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=25,
    )
    assert driver.throttle_calls == [("a@example.com", 25)]  # NOT halved again to 12


def test_daily_limit_of_two_escalates_to_pause() -> None:
    """CLOSE-3d: `2 // 2 == 1`, exactly the floor -- must escalate, not
    silently clamp to a de-facto pause with no human gate."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=2,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.throttle_calls == []
    assert driver.pause_calls == [_MAILBOX]


def test_daily_limit_of_three_escalates_to_pause() -> None:
    """CLOSE-3d: `3 // 2 == 1`, also exactly the floor."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=3,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.throttle_calls == []
    assert driver.pause_calls == [_MAILBOX]


def test_daily_limit_of_four_still_throttles_normally() -> None:
    """`4 // 2 == 2`, meaningfully above the floor -- must NOT escalate."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=4,
    )
    assert result.verdict == Verdict.THROTTLE
    assert driver.throttle_calls == [("a@example.com", 2)]
    assert driver.pause_calls == []


def test_pause_verdict_pauses_and_marks_state() -> None:
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- Idempotency: no double-pause ------------------------------------------


def test_repeated_pause_verdict_does_not_call_pause_twice() -> None:
    """Breaker trips while a previous trip is still in flight (here:
    already confirmed paused) -> idempotent, no double-pause."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    for _ in range(3):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=40,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
        )
    assert driver.pause_calls == [_MAILBOX]  # only the first evaluation actually called pause()


def test_pause_failure_marks_pause_failed_not_active() -> None:
    """CLOSE-3c: a FAILED pause is a definitive answer, but NOT "verified
    healthy" -- it must not look like ACTIVE (pristine) to a later THROTTLE
    evaluation. This deliberately changes the old assertion (`== ACTIVE`),
    which encoded the exact bug this fixes: a failed pause reset a
    mailbox's status such that a subsequent THROTTLE re-halved an
    already-throttled limit instead of staying idempotent."""
    driver = FakeDriver(pause_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_FAILED


def test_pause_unsupported_reverts_status_to_active() -> None:
    driver = FakeDriver(capabilities=frozenset({Capability.READ_STATS}))
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert result.action is not None
    assert result.action.outcome == ActionOutcome.UNSUPPORTED
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_lost_response_stays_pause_in_flight_and_propagates_the_error() -> None:
    """Provider pause succeeds but the response is lost -> reconcile on next
    tick. Here: the call raises entirely (the strongest form of "lost
    response"). Status must stay PAUSE_IN_FLIGHT, not silently revert to
    ACTIVE or jump to PAUSED -- we genuinely don't know which happened."""
    driver = FakeDriver(raise_on_pause=RateLimitExceededError("rate limited"))
    state_store = BreakerStateStore()
    with pytest.raises(RateLimitExceededError):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=40,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
        )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_IN_FLIGHT


def test_reconciliation_retries_pause_on_the_next_tick() -> None:
    """After a lost response, the next evaluation tick re-attempts the
    pause -- real pause endpoints are idempotent, so this is reconciliation,
    not a double-pause."""
    driver = FakeDriver(raise_on_pause=RateLimitExceededError("rate limited"))
    state_store = BreakerStateStore()
    with pytest.raises(RateLimitExceededError):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=40,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
        )
    # Now the provider is healthy again.
    driver.raise_on_pause = None
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert len(driver.pause_calls) == 2  # the lost attempt, then the reconciling retry
    assert result.verdict == Verdict.PAUSE
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- BreakerStateStore: never auto-resume ----------------------------------


def test_state_store_defaults_to_active() -> None:
    assert BreakerStateStore().status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_only_resume_after_human_review_moves_paused_back_to_active() -> None:
    store = BreakerStateStore()
    store.mark_paused(_MAILBOX)
    assert store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    store.resume_after_human_review(_MAILBOX)
    assert store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_evaluate_never_calls_resume_after_human_review() -> None:
    """No code path in evaluate() (or its private helper `_act`) can move a
    mailbox from PAUSED back to ACTIVE on its own -- ADR 0003."""
    import inspect

    from deliverability_guard.engine import breaker as breaker_module

    assert "resume_after_human_review" not in inspect.getsource(breaker_module.evaluate)
    assert "resume_after_human_review" not in inspect.getsource(
        breaker_module._act  # pyright: ignore[reportPrivateUsage]
    )


# --- Dry-run identity -------------------------------------------------------


def test_dry_run_decision_is_identical_to_live_except_the_flag_and_detail() -> None:
    """AGENTS.md: dry-run must produce decisions IDENTICAL to the live
    path. The only allowed difference is that the provider call itself
    isn't made -- verified here as: everything about the DECISION matches,
    and only `dry_run` and the human-readable action detail differ."""
    live_driver = FakeDriver()
    dry_driver = FakeDriver()

    live = evaluate(
        driver=live_driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    dry = evaluate(
        driver=dry_driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )

    assert live.verdict == dry.verdict
    assert live.data_state == dry.data_state
    assert live.posterior == dry.posterior
    assert live.lower_bound == dry.lower_bound
    assert live.thresholds == dry.thresholds
    assert live.sends == dry.sends
    assert live.complaints == dry.complaints
    assert live.evaluated_at == dry.evaluated_at

    assert live.action is not None
    assert dry.action is not None
    assert live.action.outcome == dry.action.outcome
    assert live.action.capability == dry.action.capability

    # The one place they're allowed to differ.
    assert live.dry_run is False
    assert dry.dry_run is True
    assert live.action.detail != dry.action.detail
    assert "[DRY RUN]" in dry.action.detail
    assert "[DRY RUN]" not in live.action.detail

    # And critically: dry-run never touched the real driver.
    assert dry_driver.pause_calls == []
    assert live_driver.pause_calls == [_MAILBOX]


# --- Compliance hard gate ----------------------------------------------


def test_compliance_gate_forces_pause_even_with_a_healthy_posterior() -> None:
    """Google telling you directly you're non-compliant outranks any
    statistical inference -- trip regardless of volume."""
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=True,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]


def test_compliance_gate_forces_pause_even_with_zero_sends() -> None:
    """The hard gate outranks volume entirely -- even INSUFFICIENT_DATA
    doesn't block it, since the compliance verdict isn't derived from
    today's send volume at all."""
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=0,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=True,
    )
    assert result.verdict == Verdict.PAUSE
    assert result.data_state == DataState.INSUFFICIENT_DATA
    assert result.posterior is None
    assert driver.pause_calls == [_MAILBOX]


def test_compliance_gate_false_does_not_change_normal_behavior() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=False,
    )
    assert result.verdict == Verdict.OK
    assert driver.pause_calls == []


# --- Hierarchical pooling wired into evaluate() (ENG-4 part 3) ------------


def test_evaluate_with_peer_group_uses_pooled_posterior() -> None:
    """Wiring check: passing `peer_group` pulls a marginal mailbox's
    POSTERIOR toward its healthy peers' posterior -- proving `evaluate()`
    actually calls `engine.posterior.pooled_posterior` rather than
    `update()` alone.

    This does NOT assert `pooled_result.lower_bound < flat_result.lower_bound`
    (the old version of this test did): CLOSE-2 fixed a bug where pooling
    could make the breaker's EFFECTIVE decision quieter than the flat
    evaluation alone, so `evaluate()` now takes the worse of the pooled and
    flat lower bounds when `peer_group` is given -- see
    `test_evaluate_peer_group_lower_bound_is_never_below_the_flat_one`
    below. The `.posterior` FIELD (used for audit/inspection) is still the
    raw pooled `BetaDistribution`, unaffected by that -- which is what this
    test checks instead."""
    driver = FakeDriver()
    healthy_peers = [GroupObservation(sends=500, complaints=0) for _ in range(40)]
    pooled_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=50,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )
    flat_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=50,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert pooled_result.posterior != flat_result.posterior
    from deliverability_guard.engine.posterior import pooled_posterior

    assert pooled_result.posterior == pooled_posterior(
        DEFAULT_PRIOR, healthy_peers, own_sends=50, own_complaints=1
    )


def test_evaluate_peer_group_lower_bound_is_never_below_the_flat_one() -> None:
    """CLOSE-2: pooling must never make the breaker's effective decision
    LESS sensitive than evaluating this mailbox's own evidence alone would
    -- `evaluate()` takes the worse (higher) of the pooled and flat lower
    bounds whenever `peer_group` is given. Here the peer group is healthy
    enough that the POOLED posterior alone would read this mailbox as
    quieter than its own evidence -- the effective lower bound must still
    match (or exceed) the flat one."""
    driver = FakeDriver()
    healthy_peers = [GroupObservation(sends=500, complaints=0) for _ in range(40)]
    pooled_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=50,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )
    from deliverability_guard.engine.posterior import update

    flat_lower_bound = update(DEFAULT_PRIOR, sends=50, complaints=1).lower_bound()
    assert pooled_result.lower_bound is not None
    assert pooled_result.lower_bound >= flat_lower_bound


def test_evaluate_peer_group_does_not_mask_a_mailbox_with_enough_of_its_own_evidence() -> None:
    """ENG-4's reproduction, at the breaker level: even wired through
    `evaluate()`, a mailbox with enough of its own bad evidence must still
    trip PAUSE, regardless of how many healthy peers it has."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    healthy_peers = [GroupObservation(sends=5000, complaints=5) for _ in range(99)]
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=250,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        peer_group=healthy_peers,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]


# --- CLOSE-2: pooling never masks a breach the flat evaluation would catch -


@pytest.mark.parametrize("own_sends", [1, 10, 50, 100, 200, 500, 1000, 5000])
def test_pooled_breach_at_the_verdict_level_is_true_wherever_flat_breach_is_true(
    own_sends: int,
) -> None:
    """The CLOSE-2 reproduction, as a property: a mailbox sending at a true
    5% complaint rate (16x Gmail's ceiling) against 999 HEALTHY peers must
    breach through `evaluate()`'s pooled path at every own-volume level
    tested, wherever it would breach evaluated flat/alone -- before this
    fix, pooling masked the breach at 100 and 200 own sends specifically
    (a healthy-looking pooled posterior diluted a genuinely bad mailbox's
    own evidence)."""
    own_complaints = round(own_sends * 0.05)
    healthy_peers = [GroupObservation(sends=5000, complaints=5) for _ in range(999)]  # 0.1% each

    flat = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=own_sends,
        complaints=own_complaints,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    pooled = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=own_sends,
        complaints=own_complaints,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )

    if flat.verdict is not Verdict.OK:
        assert pooled.verdict is not Verdict.OK, (
            f"pooling masked a breach at own_sends={own_sends}: "
            f"flat={flat.verdict.name} pooled={pooled.verdict.name}"
        )


def test_n1_at_100_percent_against_999_healthy_peers_still_does_not_breach() -> None:
    """The legitimate case pooling exists for must survive CLOSE-2's fix:
    one complaint in one send, against a domain of 999 perfectly healthy
    peers, still must not breach -- n=1 is not enough evidence to say
    anything, no matter how the peer group looks."""
    healthy_peers = [GroupObservation(sends=5000, complaints=0) for _ in range(999)]
    result = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=1,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )
    assert result.verdict == Verdict.OK


def test_evaluate_without_peer_group_is_unchanged_flat_behavior() -> None:
    """`peer_group` defaults to `None`, which must reproduce the exact flat,
    non-pooled posterior every other test in this file relies on."""
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    from deliverability_guard.engine.posterior import update

    assert result.posterior == update(DEFAULT_PRIOR, sends=5000, complaints=40)


def test_compliance_gate_is_idempotent_like_any_other_pause() -> None:
    driver = FakeDriver()
    state_store = BreakerStateStore()
    for _ in range(3):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            compliance_gate_tripped=True,
        )
    assert driver.pause_calls == [_MAILBOX]


# --- BreakerStateStore: rebuilding from the decision log (ENG-5b) ---------


def test_state_store_rebuilds_paused_status_from_the_decision_log(tmp_path: Path) -> None:
    """The reproduction: a fresh `BreakerStateStore()` (as a process restart
    would create) must not un-pause a mailbox the log says was paused."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_rebuilds_throttled_status_from_the_decision_log(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    # CLOSE3-1: the applied limit itself must also come back, not just the
    # status -- this is what keeps a *second* restart's THROTTLE evaluation
    # idempotent instead of re-halving.
    assert restored.throttled_at_limit(_MAILBOX) == 100


def test_state_store_rebuilds_active_from_a_failed_throttle_in_the_log(tmp_path: Path) -> None:
    """CLOSE3-1: a THROTTLE record whose action did NOT perform (FAILED or
    UNSUPPORTED) must rebuild as ACTIVE with no remembered limit -- mirroring
    `evaluate()`'s own live-path behaviour, where a failed throttle never
    calls `mark_throttled` in the first place."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(throttle_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert restored.throttled_at_limit(_MAILBOX) is None


def test_state_store_rebuilds_throttled_with_no_remembered_limit_from_a_pre_close3_1_record(
    tmp_path: Path,
) -> None:
    """Backward compatibility: a THROTTLE/PERFORMED record written before
    CLOSE3-1 has `applied_daily_limit=None` -- `from_log` must still rebuild
    THROTTLED status from it (as it always has), just without a remembered
    limit to restore. The very next evaluation re-derives a fresh, still
    correct halving rather than crashing or silently guessing."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    record = DecisionRecord.from_evaluation(evaluation)
    pre_close3_1_record = dataclasses.replace(record, applied_daily_limit=None)
    append_record(log_path, pre_close3_1_record)

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    assert restored.throttled_at_limit(_MAILBOX) is None


def test_state_store_rebuild_marks_pause_failed_on_a_failed_pause_attempt(tmp_path: Path) -> None:
    """Mirrors the live-path behavior (CLOSE-3c): a FAILED pause rebuilds as
    PAUSE_FAILED, not ACTIVE -- it was never verified healthy."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(pause_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_FAILED


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_later_healthy_evaluations(
    tmp_path: Path,
) -> None:
    """A paused mailbox that later reports near-zero, healthy-looking
    evidence must NOT be read back as un-paused on rebuild -- ADR 0003's
    guarantee (never auto-resume) has to survive a restart, not just an
    in-process run."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    pause_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))

    healthy_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=0,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(healthy_eval))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_a_later_unsupported_throttle(
    tmp_path: Path,
) -> None:
    """CLOSE4-1: on the live path, `_act`'s THROTTLE branch never touches
    `state_store` when the outcome is UNSUPPORTED (no `mark_active` call
    anywhere in that branch) -- so a PAUSED mailbox whose current daily
    limit later reads as unknown stays PAUSED. `from_log` used to
    unconditionally set `status[mailbox] = ACTIVE` for this outcome,
    un-pausing a mailbox the ladder is supposed to leave behind the
    human-review gate (ADR 0003) until a human calls `resume`."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    pause_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))
    assert pause_eval.verdict is Verdict.PAUSE
    assert pause_eval.action is not None
    assert pause_eval.action.outcome is ActionOutcome.PERFORMED

    unsupported_throttle_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=None,
    )
    append_record(log_path, DecisionRecord.from_evaluation(unsupported_throttle_eval))
    assert unsupported_throttle_eval.verdict is Verdict.THROTTLE
    assert unsupported_throttle_eval.action is not None
    assert unsupported_throttle_eval.action.outcome is ActionOutcome.UNSUPPORTED

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    # The streak still increments -- CLOSE3-2's escalation must not be lost
    # by fixing CLOSE4-1.
    assert restored.unsupported_throttle_streak(_MAILBOX) == 1


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_a_later_failed_throttle(
    tmp_path: Path,
) -> None:
    """Same bug, the neighbouring branch: `_act`'s THROTTLE branch only
    calls `mark_throttled` on a PERFORMED outcome -- a FAILED throttle
    (the driver itself rejected the call) never touches `state_store`
    either."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    pause_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))

    failed_driver = FakeDriver(throttle_outcome=ActionOutcome.FAILED)
    failed_throttle_eval = evaluate(
        driver=failed_driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    append_record(log_path, DecisionRecord.from_evaluation(failed_throttle_eval))
    assert failed_throttle_eval.verdict is Verdict.THROTTLE
    assert failed_throttle_eval.action is not None
    assert failed_throttle_eval.action.outcome is ActionOutcome.FAILED

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


_PERMUTATION_MOVES = (
    "PAUSE_PERFORMED",
    "THROTTLE_PERFORMED",
    "THROTTLE_UNSUPPORTED",
    "THROTTLE_FAILED",
    "OK",
    "RESUME",
)


def _apply_move(move: str, state_store: BreakerStateStore, log_path: Path) -> None:
    """Apply one move to `state_store` via a real `evaluate()` call (or, for
    RESUME, `resume_after_human_review`), appending exactly the record the
    live path itself would append. Shared verbatim between the live
    reference sequence and the sequence being replayed into the log --
    the only difference between the two is WHEN `from_log` rebuilds from
    those records, never what produced them."""
    if move == "RESUME":
        state_store.resume_after_human_review(_MAILBOX)
        append_resume_record(
            log_path,
            ResumeRecord(
                resumed_at=_NOW,
                provider=_MAILBOX.provider,
                mailbox_id=_MAILBOX.mailbox_id,
                resumed_by="kate",
            ),
        )
        return

    driver_kwargs: dict[str, object] = {}
    eval_kwargs: dict[str, object] = {"sends": 5000, "complaints": 40}
    if move == "PAUSE_PERFORMED":
        driver_kwargs = {"pause_outcome": ActionOutcome.PERFORMED}
        eval_kwargs = {"sends": 5000, "complaints": 40}
    elif move == "THROTTLE_PERFORMED":
        driver_kwargs = {"throttle_outcome": ActionOutcome.PERFORMED}
        eval_kwargs = {"sends": 20_000, "complaints": 30, "current_daily_limit": 100}
    elif move == "THROTTLE_UNSUPPORTED":
        eval_kwargs = {"sends": 20_000, "complaints": 30, "current_daily_limit": None}
    elif move == "THROTTLE_FAILED":
        driver_kwargs = {"throttle_outcome": ActionOutcome.FAILED}
        eval_kwargs = {"sends": 20_000, "complaints": 30, "current_daily_limit": 100}
    elif move == "OK":
        eval_kwargs = {"sends": 5000, "complaints": 0}
    else:  # pragma: no cover -- exhaustive over _PERMUTATION_MOVES
        raise AssertionError(f"unhandled move {move!r}")

    driver = FakeDriver(**driver_kwargs)  # type: ignore[arg-type]
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        **eval_kwargs,  # type: ignore[arg-type]
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))


@pytest.mark.parametrize("sequence", list(itertools.permutations(_PERMUTATION_MOVES)))
def test_from_log_replay_matches_the_live_path_over_every_move_ordering(
    sequence: tuple[str, ...], tmp_path: Path
) -> None:
    """The invariant CLOSE4-1's bug violated, tested directly rather than
    via one or two hand-picked sequences: replaying the decision log must
    always reproduce the SAME status a single uninterrupted in-process run
    through the identical sequence of evaluations would have reached.
    Every permutation of (PAUSE/PERFORMED, THROTTLE/PERFORMED,
    THROTTLE/UNSUPPORTED, THROTTLE/FAILED, OK, RESUME) is tried -- 720
    orderings, each applied once to a live `state_store` and once (via the
    same calls, writing to the same log) followed by a fresh
    `BreakerStateStore.from_log` rebuild at the very end."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    for move in sequence:
        _apply_move(move, live_store, log_path)

    replayed_store = BreakerStateStore.from_log(log_path)
    assert replayed_store.status_of(_MAILBOX) == live_store.status_of(_MAILBOX), sequence


def test_state_store_rebuild_reverts_to_active_on_an_unsupported_pause_attempt(
    tmp_path: Path,
) -> None:
    """A definitively UNSUPPORTED pause -- the provider can never pause this
    target -- rebuilds as ACTIVE, distinct from PAUSE_FAILED (which is for a
    transient FAILED outcome, not a structural one)."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(capabilities=frozenset({Capability.READ_STATS}))
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_from_log_recovers_a_throttled_mailbox_after_five_healthy_restarts(
    tmp_path: Path,
) -> None:
    """CLOSE3-3: a THROTTLE followed by sustained healthy evaluations must
    clear the persisted THROTTLED status, not just the in-process one --
    `from_log` leaving OK verdicts alone (as it did before this fix) meant a
    fully recovered mailbox that happened to restart mid-recovery read
    THROTTLED forever, and no command could clear it. Five SEPARATE
    restarts (a fresh `BreakerStateStore.from_log` each time, per the
    session rule that every state-related test gets a sibling that
    restarts), not five in-process ticks."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    throttle_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    append_record(log_path, DecisionRecord.from_evaluation(throttle_eval))
    assert (
        BreakerStateStore.from_log(log_path).status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    )

    for _ in range(5):
        restarted_store = BreakerStateStore.from_log(log_path)
        ok_eval = evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=restarted_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=50,
        )
        assert ok_eval.verdict == Verdict.OK
        append_record(log_path, DecisionRecord.from_evaluation(ok_eval))

    final = BreakerStateStore.from_log(log_path)
    assert final.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert final.throttled_at_limit(_MAILBOX) is None


def test_state_store_from_log_with_no_file_yet_is_an_empty_active_store(tmp_path: Path) -> None:
    """No log file at all is a genuinely new deployment -- every mailbox
    correctly defaults to ACTIVE. This is the "no record" case, distinct
    from "couldn't read the record" below."""
    missing = tmp_path / "does-not-exist.jsonl"
    store = BreakerStateStore.from_log(missing)
    assert store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_from_log_fails_closed_on_an_unreadable_log(tmp_path: Path) -> None:
    """A log that EXISTS but can't be parsed must not silently produce an
    empty (every-mailbox-ACTIVE) store -- that would be indistinguishable
    from "no record" and could auto-resume a mailbox that was actually
    PAUSED. Fail loudly instead."""
    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text("not valid json\n")
    with pytest.raises(BreakerStateStoreLoadError):
        BreakerStateStore.from_log(log_path)


# --- CLOSE-4: resume durability, dry-run non-persistence, empty log --------


def test_state_store_rebuilds_active_status_from_a_resume_record_after_pause(
    tmp_path: Path,
) -> None:
    """The CLOSE-4 reproduction, at the breaker level: a paused mailbox with
    a `ResumeRecord` after it in the log must rebuild as ACTIVE."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))
    append_resume_record(
        log_path,
        ResumeRecord(
            resumed_at=_NOW, provider="fake", mailbox_id="a@example.com", resumed_by="kate"
        ),
    )

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_rebuild_re_pauses_after_a_later_resume_if_evaluated_again(
    tmp_path: Path,
) -> None:
    """A resume is a point-in-time event, not a permanent exemption: a
    mailbox resumed and then paused AGAIN later must rebuild as PAUSED --
    `from_log` must apply events strictly in file order, not just check
    "was there ever a resume record."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    first_pause = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(first_pause))
    append_resume_record(
        log_path,
        ResumeRecord(
            resumed_at=_NOW, provider="fake", mailbox_id="a@example.com", resumed_by="kate"
        ),
    )
    second_pause = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(second_pause))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_from_log_skips_a_dry_run_pause_record(tmp_path: Path) -> None:
    """CLOSE-4b's reproduction: a dry-run PAUSE record must never rebuild as
    PAUSED -- the real (fake) driver was never touched."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert evaluation.verdict == Verdict.PAUSE
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_from_log_dry_run_pause_then_real_pause_of_another_mailbox(
    tmp_path: Path,
) -> None:
    """A dry-run pause of one mailbox must not affect a real pause of a
    different mailbox recorded in the same log."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    other_mailbox = MailboxRef(provider="fake", mailbox_id="b@example.com")
    dry_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(dry_eval))
    real_eval = evaluate(
        driver=driver,
        mailbox=other_mailbox,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(real_eval))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert restored.status_of(other_mailbox) == MailboxBreakerStatus.PAUSED


def test_state_store_from_log_fails_closed_on_an_empty_log(tmp_path: Path) -> None:
    """A log that EXISTS but is zero bytes is ambiguous between "nothing has
    ever happened" and "the log was truncated" -- fail loudly, the same as
    any other unreadable log, rather than rebuilding an all-ACTIVE store."""
    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text("")
    with pytest.raises(BreakerStateStoreLoadError):
        BreakerStateStore.from_log(log_path)
