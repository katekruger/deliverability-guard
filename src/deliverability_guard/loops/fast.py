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

from dataclasses import dataclass
from datetime import datetime

from deliverability_guard.engine.breaker import (
    BreakerEvaluation,
    BreakerStateStore,
    ThresholdLadder,
    evaluate,
)
from deliverability_guard.engine.posterior import BetaDistribution
from deliverability_guard.providers.base import (
    MailboxRef,
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
