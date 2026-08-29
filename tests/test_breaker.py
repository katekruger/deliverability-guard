"""Tests for engine/breaker.py: the ladder, idempotent pause, and dry-run identity."""

from datetime import UTC, datetime

import pytest

from deliverability_guard.engine.breaker import (
    DEFAULT_LADDER,
    BreakerStateStore,
    MailboxBreakerStatus,
    ThresholdLadder,
    ThresholdStore,
    Verdict,
    evaluate,
    rung,
)
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
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


def test_throttle_never_reduces_below_the_floor_of_one() -> None:
    driver = FakeDriver()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=1,
    )
    assert driver.throttle_calls == [("a@example.com", 1)]


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


def test_pause_failure_reverts_status_to_active() -> None:
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
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


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
