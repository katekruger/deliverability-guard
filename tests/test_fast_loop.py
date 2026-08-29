"""Tests for loops/fast.py, including the end-to-end webhook -> evaluation
-> throttle path in dry-run required by Prompt 3's definition of done."""

from datetime import UTC, datetime

from deliverability_guard.engine.breaker import DEFAULT_LADDER, BreakerStateStore, Verdict
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.loops.fast import FastLoopSignal, evaluate_signal
from deliverability_guard.providers.base import MailboxRef, WebhookEvent, WebhookLedger
from fixtures.fake_driver import FakeDriver

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_MAILBOX = MailboxRef(provider="fake", mailbox_id="a@example.com")


def _bounce_event(event_id: str) -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id,
        provider="fake",
        event_type="bounce",
        occurred_at=_NOW,
        mailbox=_MAILBOX,
    )


def test_webhook_to_evaluation_to_throttle_end_to_end_in_dry_run() -> None:
    driver = FakeDriver()
    signal = FastLoopSignal(mailbox=_MAILBOX, event=_bounce_event("evt-1"))

    result = evaluate_signal(
        signal,
        driver=driver,
        ledger=WebhookLedger(),
        cumulative_sends=20_000,
        cumulative_complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        current_daily_limit=100,
    )

    assert result is not None
    assert result.verdict == Verdict.THROTTLE
    assert result.dry_run is True
    assert result.action is not None
    assert "[DRY RUN]" in result.action.detail
    # Dry-run: the real driver's throttle() was never called.
    assert driver.throttle_calls == []


def test_a_redelivered_webhook_is_not_evaluated_twice() -> None:
    """Duplicate webhook delivery -> idempotent by event id."""
    driver = FakeDriver()
    ledger = WebhookLedger()
    signal = FastLoopSignal(mailbox=_MAILBOX, event=_bounce_event("evt-1"))

    first = evaluate_signal(
        signal,
        driver=driver,
        ledger=ledger,
        cumulative_sends=5000,
        cumulative_complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    second = evaluate_signal(
        signal,  # the exact same event, redelivered
        driver=driver,
        ledger=ledger,
        cumulative_sends=5000,
        cumulative_complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )

    assert first is not None
    assert second is None


def test_a_different_event_id_for_the_same_mailbox_is_still_evaluated() -> None:
    driver = FakeDriver()
    ledger = WebhookLedger()

    first = evaluate_signal(
        FastLoopSignal(mailbox=_MAILBOX, event=_bounce_event("evt-1")),
        driver=driver,
        ledger=ledger,
        cumulative_sends=50,
        cumulative_complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    second = evaluate_signal(
        FastLoopSignal(mailbox=_MAILBOX, event=_bounce_event("evt-2")),
        driver=driver,
        ledger=ledger,
        cumulative_sends=51,
        cumulative_complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )

    assert first is not None
    assert second is not None
