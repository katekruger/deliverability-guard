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


# --- CLOSE-1: current_daily_limit aggregation ------------------------------


def test_aggregate_takes_the_most_recent_days_current_daily_limit() -> None:
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    totals = aggregate_mailbox_stats(
        [
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 30),
                sends=100,
                bounces=0,
                current_daily_limit=200,
            ),
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=100,
                bounces=0,
                current_daily_limit=100,
            ),
        ]
    )
    assert totals[0].current_daily_limit == 100


def test_aggregate_current_daily_limit_defaults_to_none_when_never_reported() -> None:
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    totals = aggregate_mailbox_stats(
        [MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 31), sends=100, bounces=0)]
    )
    assert totals[0].current_daily_limit is None


def test_aggregate_current_daily_limit_out_of_order_days_still_takes_the_latest() -> None:
    """Rows aren't guaranteed to arrive in day order -- the LATEST day's
    limit must win regardless of iteration order."""
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    totals = aggregate_mailbox_stats(
        [
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=100,
                bounces=0,
                current_daily_limit=100,
            ),
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 30),
                sends=100,
                bounces=0,
                current_daily_limit=200,
            ),
        ]
    )
    assert totals[0].current_daily_limit == 100


def test_domain_of_a_mailbox_id_with_no_at_sign_is_itself() -> None:
    from deliverability_guard.loops.fast import _domain_of  # pyright: ignore[reportPrivateUsage]

    assert _domain_of("not-an-email") == "not-an-email"


# --- CLOSE-1: evaluate_all_mailboxes wires peer_group, current_daily_limit,
# and CUSUM into the chokepoint both `cli.cmd_check` and `loops.controller`
# share -- the fix for the audit finding that pooled_posterior, cusum_step,
# and evaluate_stream had callers that nothing itself called in production.


def test_evaluate_all_mailboxes_throttles_a_mailbox_given_a_current_daily_limit() -> None:
    """Before this wiring, THROTTLE could only ever report itself
    UNSUPPORTED through this path -- `current_daily_limit` was never
    passed. Reproduces CLOSE-3a end to end through the real chokepoint."""
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=20_000,
                bounces=30,
                current_daily_limit=100,
            )
        ]
    )

    (result,) = evaluate_all_mailboxes(
        driver=driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )

    assert result.verdict == Verdict.THROTTLE
    assert result.action is not None
    from deliverability_guard.providers.base import ActionOutcome

    assert result.action.outcome is ActionOutcome.PERFORMED
    assert driver.throttle_calls == [("a@example.com", 50)]


def test_evaluate_all_mailboxes_pools_peers_on_the_same_domain() -> None:
    """A marginal mailbox (n=50, 1 complaint) on a domain with 40 other
    healthy, real-volume mailboxes gets a different POSTERIOR than it would
    alone -- proof `evaluate_all_mailboxes` actually builds and passes a
    same-domain `peer_group`, not `None` (the `.posterior` field is
    unaffected by CLOSE-2's worse-of-two lower-bound fix; only the
    resulting verdict/lower_bound is)."""
    domain = "example.com"
    marginal = MailboxRef(provider="fake", mailbox_id=f"marginal@{domain}")
    stats = [
        MailboxDayStats(mailbox=marginal, day=date(2025, 12, 31), sends=50, bounces=1),
    ]
    for i in range(40):
        peer = MailboxRef(provider="fake", mailbox_id=f"peer{i}@{domain}")
        stats.append(MailboxDayStats(mailbox=peer, day=date(2025, 12, 31), sends=500, bounces=0))
    driver = FakeDriver(stats_to_return=stats)

    pooled_results = evaluate_all_mailboxes(
        driver=driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    marginal_result = next(r for r in pooled_results if r.mailbox == marginal)

    flat_driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=marginal, day=date(2025, 12, 31), sends=50, bounces=1)
        ]
    )
    (flat_result,) = evaluate_all_mailboxes(
        driver=flat_driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )

    assert marginal_result.posterior != flat_result.posterior


def test_evaluate_all_mailboxes_does_not_pool_across_different_domains() -> None:
    """Two mailboxes on DIFFERENT domains must not pool with each other --
    peer groups are per-domain, not global."""
    mailbox_a = MailboxRef(provider="fake", mailbox_id="a@one.example.com")
    mailbox_b = MailboxRef(provider="fake", mailbox_id="b@two.example.com")
    driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=mailbox_a, day=date(2025, 12, 31), sends=50, bounces=1),
            MailboxDayStats(mailbox=mailbox_b, day=date(2025, 12, 31), sends=500, bounces=0),
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
    result_a = next(r for r in results if r.mailbox == mailbox_a)

    from deliverability_guard.engine.posterior import update

    assert result_a.posterior == update(DEFAULT_PRIOR, sends=50, complaints=1)


def test_evaluate_all_mailboxes_respects_a_configured_max_pooled_ess() -> None:
    """A deployment can tune the pooling cap (CLOSE-1 item 1): a smaller
    `max_pooled_ess` bounds a large peer group's influence more tightly."""
    domain = "example.com"
    target = MailboxRef(provider="fake", mailbox_id=f"target@{domain}")
    stats = [MailboxDayStats(mailbox=target, day=date(2025, 12, 31), sends=200, bounces=10)]
    for i in range(99):
        peer = MailboxRef(provider="fake", mailbox_id=f"peer{i}@{domain}")
        stats.append(MailboxDayStats(mailbox=peer, day=date(2025, 12, 31), sends=5000, bounces=5))

    default_driver = FakeDriver(stats_to_return=list(stats))
    (default_result,) = [
        r
        for r in evaluate_all_mailboxes(
            driver=default_driver,
            since=date(2025, 12, 31),
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=_NOW,
        )
        if r.mailbox == target
    ]

    small_cap_driver = FakeDriver(stats_to_return=list(stats))
    (small_cap_result,) = [
        r
        for r in evaluate_all_mailboxes(
            driver=small_cap_driver,
            since=date(2025, 12, 31),
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=_NOW,
            max_pooled_ess=50.0,
        )
        if r.mailbox == target
    ]

    assert default_result.posterior != small_cap_result.posterior


def test_evaluate_all_mailboxes_runs_cusum_when_given_state() -> None:
    """`cusum_step` actually executes through this chokepoint when the
    caller opts in with `cusum_states` -- proof this is wired, not just
    reachable in principle."""
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 31), sends=50, bounces=5)
        ]
    )
    cusum_states: dict[MailboxRef, CusumState] = {}
    alarms: list[MailboxRef] = []

    evaluate_all_mailboxes(
        driver=driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        cusum_states=cusum_states,
        cusum_target_rate=0.001,
        cusum_slack=0.001,
        cusum_threshold=1.0,
        on_cusum_alarm=lambda mailbox_ref, _result: alarms.append(mailbox_ref),
    )

    assert mailbox in cusum_states
    assert alarms == [mailbox]


def test_evaluate_all_mailboxes_runs_cusum_without_an_alarm_callback() -> None:
    """`on_cusum_alarm` is optional -- an alarm must not raise just because
    nothing is listening for it."""
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 31), sends=50, bounces=5)
        ]
    )
    cusum_states: dict[MailboxRef, CusumState] = {}

    evaluate_all_mailboxes(
        driver=driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        cusum_states=cusum_states,
        cusum_target_rate=0.001,
        cusum_slack=0.001,
        cusum_threshold=1.0,
    )
    assert mailbox in cusum_states


def test_evaluate_all_mailboxes_skips_cusum_without_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`cusum_states=None` (the default) is an intentional opt-out, not an
    error -- CUSUM must not run at all."""
    import deliverability_guard.loops.fast as fast_module

    calls: list[object] = []
    monkeypatch.setattr(fast_module, "cusum_step", lambda *a, **k: calls.append(1))  # type: ignore[arg-type]

    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 31), sends=50, bounces=5)
        ]
    )
    evaluate_all_mailboxes(
        driver=driver,
        since=date(2025, 12, 31),
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert calls == []
