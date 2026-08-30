"""Fast loop: seconds-to-minutes reaction to leading indicators.

Bounce webhooks, SMTP codes (4.7.31, 4.7.32, 5.7.515, 5.7.x), mailbox
disconnect events, and TLS delivery failures feed this loop. Complaint data
lags 24h-3 days, so the fast loop must act on what is observable now rather
than waiting on a lagging signal (BUILD-PLAN.md §5). For a 50/day mailbox
that lag is 150 messages already gone by the time a lagging signal would
have fired; for a 40-mailbox farm, 6,000.

This module's job is narrow and deliberately so: decide whether a signal is
a fresh event worth evaluating at all (via the same `WebhookLedger` used by
provider drivers, so a redelivered webhook doesn't trigger a second
evaluation), then hand off to `engine.breaker.evaluate` for the actual
decision. It does not own send/complaint accounting -- the caller supplies
the mailbox's current cumulative counts, e.g. from a running tally kept
elsewhere as webhooks arrive.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from deliverability_guard.engine.breaker import (
    BreakerEvaluation,
    BreakerStateStore,
    ThresholdLadder,
    evaluate,
)
from deliverability_guard.engine.changepoint import CusumResult, CusumState, cusum_step
from deliverability_guard.engine.posterior import BetaDistribution
from deliverability_guard.providers.base import (
    MailboxDayStats,
    MailboxRef,
    MailboxStatus,
    ProviderDriver,
    WebhookEvent,
    WebhookLedger,
)


@dataclass(frozen=True, slots=True)
class FastLoopSignal:
    """A leading indicator that something may be wrong right now."""

    mailbox: MailboxRef
    event: WebhookEvent


def evaluate_signal(
    signal: FastLoopSignal,
    *,
    driver: ProviderDriver,
    ledger: WebhookLedger,
    cumulative_sends: int,
    cumulative_complaints: int,
    prior: BetaDistribution,
    thresholds: ThresholdLadder,
    state_store: BreakerStateStore,
    dry_run: bool,
    now: datetime,
    current_daily_limit: int | None = None,
) -> BreakerEvaluation | None:
    """Returns `None` if `signal.event` is a redelivery already processed
    via `ledger` -- there is nothing new to evaluate. Otherwise runs a full
    breaker evaluation using the caller-supplied cumulative counts.

    `dry_run` has no default here either, for the same reason as
    `engine.breaker.evaluate`: every call site decides explicitly.
    """
    if not ledger.accept(signal.event):
        return None
    return evaluate(
        driver=driver,
        mailbox=signal.mailbox,
        sends=cumulative_sends,
        complaints=cumulative_complaints,
        prior=prior,
        thresholds=thresholds,
        state_store=state_store,
        dry_run=dry_run,
        now=now,
        current_daily_limit=current_daily_limit,
    )


def evaluate_signal_with_trend(
    signal: FastLoopSignal,
    *,
    driver: ProviderDriver,
    ledger: WebhookLedger,
    cumulative_sends: int,
    cumulative_complaints: int,
    period_sends: int,
    period_complaints: int,
    prior: BetaDistribution,
    thresholds: ThresholdLadder,
    state_store: BreakerStateStore,
    dry_run: bool,
    now: datetime,
    cusum_state: CusumState,
    target_rate: float,
    slack: float,
    threshold: float,
    current_daily_limit: int | None = None,
) -> tuple[BreakerEvaluation | None, CusumResult]:
    """`evaluate_signal` plus sequential change detection (`engine.changepoint`)
    on the same period's evidence -- BUILD-PLAN.md §6's "catches a trend
    shift faster than any fixed-window rate" running alongside, not instead
    of, the breaker's own posterior-based ladder.

    `period_sends`/`period_complaints` are this evaluation's own slice of
    volume (e.g. today's count), distinct from `cumulative_sends`/
    `cumulative_complaints` which the breaker's posterior update needs --
    CUSUM tracks a running deviation period over period, so it needs the
    period's own count, not a lifetime total.

    On a redelivered event (nothing new to evaluate), CUSUM is skipped
    entirely and `cusum_state` is returned unchanged -- a redelivery carries
    no new evidence for either half of this function.
    """
    evaluation = evaluate_signal(
        signal,
        driver=driver,
        ledger=ledger,
        cumulative_sends=cumulative_sends,
        cumulative_complaints=cumulative_complaints,
        prior=prior,
        thresholds=thresholds,
        state_store=state_store,
        dry_run=dry_run,
        now=now,
        current_daily_limit=current_daily_limit,
    )
    if evaluation is None:
        return None, CusumResult(state=cusum_state, alarmed=False)
    trend = cusum_step(
        cusum_state,
        period_sends,
        period_complaints,
        target_rate=target_rate,
        slack=slack,
        threshold=threshold,
    )
    return evaluation, trend


@dataclass(frozen=True, slots=True)
class MailboxTotals:
    """One mailbox's summed sends/complaints over an aggregation window."""

    mailbox: MailboxRef
    sends: int
    complaints: int


def aggregate_mailbox_stats(day_stats: Iterable[MailboxDayStats]) -> list[MailboxTotals]:
    """Sum sends/bounces per mailbox across a driver's daily stats.

    A `DISCONNECTED` day is an outage, not evidence (see
    `providers.base.MailboxStatus`), and is excluded from the aggregate
    entirely rather than folded in as 0 sends/0 bounces -- the same
    missing-data-as-zero coercion AGENTS.md prohibits everywhere else in
    this project. Returned sorted by mailbox id for stable, diffable
    output regardless of the order a driver happens to report stats in.
    """
    totals: dict[MailboxRef, tuple[int, int]] = {}
    for stat in day_stats:
        if stat.status is MailboxStatus.DISCONNECTED:
            continue
        prior_sends, prior_complaints = totals.get(stat.mailbox, (0, 0))
        totals[stat.mailbox] = (prior_sends + stat.sends, prior_complaints + stat.bounces)
    return [
        MailboxTotals(mailbox=mailbox, sends=sends, complaints=complaints)
        for mailbox, (sends, complaints) in sorted(totals.items(), key=lambda kv: kv[0].mailbox_id)
    ]


def evaluate_all_mailboxes(
    *,
    driver: ProviderDriver,
    since: date,
    prior: BetaDistribution,
    thresholds: ThresholdLadder,
    state_store: BreakerStateStore,
    dry_run: bool,
    now: datetime,
) -> list[BreakerEvaluation]:
    """Pull every mailbox's stats since `since`, aggregate, and evaluate
    each one through the breaker.

    This is the shared pull-based evaluation path used by both `cli.cmd_check`
    (one-shot) and `loops.controller`'s fast tick (continuous polling), so
    the two cannot drift apart from each other the way a hand-duplicated
    aggregation loop in each caller eventually would.
    """
    totals = aggregate_mailbox_stats(driver.read_mailbox_stats(since))
    return [
        evaluate(
            driver=driver,
            mailbox=total.mailbox,
            sends=total.sends,
            complaints=total.complaints,
            prior=prior,
            thresholds=thresholds,
            state_store=state_store,
            dry_run=dry_run,
            now=now,
        )
        for total in totals
    ]
