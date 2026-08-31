"""`FastLoopSignal`/`evaluate_signal`: per-webhook-event evaluation for a
fast loop that reacts to pushed webhooks rather than polling.

STATUS (CLOSE3-5): moved here from `loops/fast.py`. Nothing in this
codebase accepts an inbound webhook at all -- `loops.controller`'s own
module docstring says so explicitly: it runs a deliberately POLLING fast
loop instead, via `loops.fast.evaluate_all_mailboxes`, "a strictly weaker
but honest substitute" until a real webhook receiver (an HTTP server,
per-provider signature verification) exists. `evaluate_signal` had zero
production callers before this move, same as its already-deleted sibling
`evaluate_signal_with_trend`.

Unlike `evaluate_signal_with_trend` (deleted outright, because CUSUM's
per-period evidence model fits `evaluate_all_mailboxes`'s pull-based tick
far better than a per-event signal), `evaluate_signal` itself is exactly
the shape a real webhook receiver would want once one exists: decide
whether an event is a fresh delivery worth evaluating at all (via the same
`providers.base.WebhookLedger` provider drivers already use), then hand off
to `engine.breaker.evaluate` for the actual decision. It is quarantined
here, not deleted, because it is real, correct, and load-bearing logic for
a feature this project intends to ship -- just not yet wired to anything
that can call it.

Promote this back to `loops/fast.py` once a real webhook receiver exists to
call it.
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
