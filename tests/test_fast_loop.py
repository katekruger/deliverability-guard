"""Tests for loops/fast.py, including the end-to-end webhook -> evaluation
-> throttle path in dry-run required by Prompt 3's definition of done."""

from datetime import UTC, date, datetime

from deliverability_guard.engine.breaker import DEFAULT_LADDER, BreakerStateStore, Verdict
from deliverability_guard.engine.changepoint import CusumState
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.loops.fast import (
    FastLoopSignal,
    aggregate_mailbox_stats,
    evaluate_all_mailboxes,
    evaluate_signal,
    evaluate_signal_with_trend,
)
from deliverability_guard.providers.base import (
    MailboxDayStats,
    MailboxRef,
    MailboxStatus,
    WebhookEvent,
    WebhookLedger,
)
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


# --- evaluate_signal_with_trend: CUSUM wired into the fast loop -----------


def test_evaluate_signal_with_trend_runs_cusum_alongside_the_breaker() -> None:
    """This is `cusum_step`'s first production caller: a real upward shift
    in one period (5 complaints in 50 sends, far above `target_rate`) must
    alarm."""
    driver = FakeDriver()
    signal = FastLoopSignal(mailbox=_MAILBOX, event=_bounce_event("evt-1"))

    evaluation, trend = evaluate_signal_with_trend(
        signal,
        driver=driver,
        ledger=WebhookLedger(),
        cumulative_sends=50,
        cumulative_complaints=5,
        period_sends=50,
        period_complaints=5,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        cusum_state=CusumState(),
        target_rate=0.001,
        slack=0.001,
        threshold=1.0,
    )

    assert evaluation is not None
    assert trend.alarmed is True


def test_evaluate_signal_with_trend_skips_cusum_on_a_redelivered_event() -> None:
    """A redelivered webhook carries no new period evidence -- CUSUM must
    not be re-run on it, exactly like the breaker evaluation itself."""
    driver = FakeDriver()
    ledger = WebhookLedger()
    signal = FastLoopSignal(mailbox=_MAILBOX, event=_bounce_event("evt-1"))

    evaluate_signal_with_trend(
        signal,
        driver=driver,
        ledger=ledger,
        cumulative_sends=50,
        cumulative_complaints=0,
        period_sends=50,
        period_complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        cusum_state=CusumState(),
        target_rate=0.001,
        slack=0.001,
        threshold=1.0,
    )
    evaluation, trend = evaluate_signal_with_trend(
        signal,  # redelivered
        driver=driver,
        ledger=ledger,
        cumulative_sends=50,
        cumulative_complaints=0,
        period_sends=999,
        period_complaints=999,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        cusum_state=CusumState(),
        target_rate=0.001,
        slack=0.001,
        threshold=1.0,
    )

    assert evaluation is None
    assert trend.alarmed is False
    assert trend.state.cumulative == 0.0


# --- aggregate_mailbox_stats / evaluate_all_mailboxes ---------------------
#
# Factored out of cli.cmd_check so the one-shot `check` command and the
# continuous daemon (loops/controller.py) share one aggregation-and-
# evaluation path and cannot drift apart from each other.


def test_aggregate_sums_sends_and_bounces_per_mailbox() -> None:
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    totals = aggregate_mailbox_stats(
        [
            MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 30), sends=2500, bounces=20),
            MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 31), sends=2500, bounces=20),
        ]
    )
    assert len(totals) == 1
    assert totals[0].mailbox == mailbox
    assert totals[0].sends == 5000
    assert totals[0].complaints == 40


def test_aggregate_excludes_disconnected_days() -> None:
    """A DISCONNECTED day is an outage, not evidence -- see
    providers.base.MailboxStatus. It must not be folded into the aggregate
    as 0 sends/0 bounces."""
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    totals = aggregate_mailbox_stats(
        [
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=0,
                bounces=0,
                status=MailboxStatus.DISCONNECTED,
            )
        ]
    )
    assert totals == []


def test_aggregate_sorts_by_mailbox_id_for_stable_output() -> None:
    mailbox_b = MailboxRef(provider="fake", mailbox_id="b@example.com")
    mailbox_a = MailboxRef(provider="fake", mailbox_id="a@example.com")
    totals = aggregate_mailbox_stats(
        [
            MailboxDayStats(mailbox=mailbox_b, day=date(2025, 12, 31), sends=1, bounces=0),
            MailboxDayStats(mailbox=mailbox_a, day=date(2025, 12, 31), sends=1, bounces=0),
        ]
    )
    assert [t.mailbox for t in totals] == [mailbox_a, mailbox_b]


def test_evaluate_all_mailboxes_evaluates_every_mailbox_the_driver_reports() -> None:
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 31), sends=5000, bounces=0)
        ]
    )

    results = evaluate_all_mailboxes(
        driver=driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )

    assert len(results) == 1
    assert results[0].mailbox == mailbox
    assert results[0].verdict == Verdict.OK


def test_evaluate_all_mailboxes_with_no_stats_is_an_empty_list() -> None:
    driver = FakeDriver(stats_to_return=[])
    results = evaluate_all_mailboxes(
        driver=driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert results == []
