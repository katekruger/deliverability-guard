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

`evaluate_all_mailboxes` is the OTHER, pull-based entry point, and the one
that matters most: it is the shared chokepoint `cli.cmd_check` and
`loops.controller.run`'s fast tick both call, so it is the single place
where a mailbox's peer group (for hierarchical pooling), current daily
limit (for THROTTLE), and CUSUM trend state must all be assembled and
threaded into `engine.breaker.evaluate` for either of those callers to
actually exercise `engine.posterior.pooled_posterior` or
`engine.changepoint.cusum_step` in production (CLOSE-1: before this,
`evaluate_all_mailboxes` called `evaluate()` with neither `peer_group` nor
`current_daily_limit`, so a real `check`/`run` never pooled and never
throttled).
"""

from collections.abc import Callable, Iterable, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from deliverability_guard.engine.breaker import (
    BreakerEvaluation,
    BreakerStateStore,
    ThresholdLadder,
    evaluate,
)
from deliverability_guard.engine.changepoint import CusumResult, CusumState, cusum_step
from deliverability_guard.engine.posterior import (
    DEFAULT_MAX_POOLED_ESS,
    BetaDistribution,
    GroupObservation,
)
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

    There used to be a companion `evaluate_signal_with_trend` that ran
    `engine.changepoint.cusum_step` alongside this. It had zero production
    callers (CLOSE-1) -- nothing in this codebase yet accepts an inbound
    webhook at all (see `loops.controller`'s module docstring), so nothing
    called this function either, let alone the trend variant. It was
    deleted rather than wired up: CUSUM's per-period evidence model fits
    `evaluate_all_mailboxes`'s pull-based tick, below, far more naturally
    than a per-webhook-event signal, and that's where it's wired now.
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


@dataclass(frozen=True, slots=True)
class MailboxTotals:
    """One mailbox's summed sends/complaints over an aggregation window,
    plus its most recently reported daily limit."""

    mailbox: MailboxRef
    sends: int
    complaints: int
    current_daily_limit: int | None = None
    """The `current_daily_limit` from the most recent ACTIVE day's stats, or
    `None` if no day reported one. A limit is a point-in-time setting, not
    something to sum across days the way sends/complaints are -- taking the
    latest is the closest a multi-day aggregation window can get to "the
    limit right now." A `DISCONNECTED` day's limit is ignored, same as its
    sends/bounces (see `aggregate_mailbox_stats`)."""


def aggregate_mailbox_stats(day_stats: Iterable[MailboxDayStats]) -> list[MailboxTotals]:
    """Sum sends/bounces per mailbox across a driver's daily stats, and take
    each mailbox's most recent reported daily limit.

    A `DISCONNECTED` day is an outage, not evidence (see
    `providers.base.MailboxStatus`), and is excluded from the aggregate
    entirely rather than folded in as 0 sends/0 bounces -- the same
    missing-data-as-zero coercion AGENTS.md prohibits everywhere else in
    this project. Returned sorted by mailbox id for stable, diffable
    output regardless of the order a driver happens to report stats in.
    """
    totals: dict[MailboxRef, tuple[int, int]] = {}
    latest_limit: dict[MailboxRef, tuple[date, int | None]] = {}
    for stat in day_stats:
        if stat.status is MailboxStatus.DISCONNECTED:
            continue
        prior_sends, prior_complaints = totals.get(stat.mailbox, (0, 0))
        totals[stat.mailbox] = (prior_sends + stat.sends, prior_complaints + stat.bounces)
        seen_day, _ = latest_limit.get(stat.mailbox, (stat.day, None))
        if stat.mailbox not in latest_limit or stat.day >= seen_day:
            latest_limit[stat.mailbox] = (stat.day, stat.current_daily_limit)
    return [
        MailboxTotals(
            mailbox=mailbox,
            sends=sends,
            complaints=complaints,
            current_daily_limit=latest_limit[mailbox][1],
        )
        for mailbox, (sends, complaints) in sorted(totals.items(), key=lambda kv: kv[0].mailbox_id)
    ]


def _domain_of(mailbox_id: str) -> str:
    """The part of a mailbox address after the last "@", used to group
    peers for hierarchical pooling (CLOSE-1: "every mailbox's peers are the
    other mailboxes in the same sending domain"). A mailbox id with no "@"
    at all (never expected in practice, but not this function's job to
    reject) is its own domain of one -- it simply gets no peers, the same
    as `peer_group=None` did before this wiring existed."""
    if "@" not in mailbox_id:
        return mailbox_id
    return mailbox_id.rsplit("@", 1)[-1]


def _peer_groups(totals: Sequence[MailboxTotals]) -> dict[MailboxRef, list[GroupObservation]]:
    """Each mailbox's peer group: every OTHER mailbox in `totals` sharing
    its sending domain (leave-one-out, matching
    `engine.posterior.pooled_posterior`'s own contract)."""
    by_domain: dict[str, list[MailboxTotals]] = {}
    for total in totals:
        by_domain.setdefault(_domain_of(total.mailbox.mailbox_id), []).append(total)
    groups: dict[MailboxRef, list[GroupObservation]] = {}
    for members in by_domain.values():
        for member in members:
            groups[member.mailbox] = [
                GroupObservation(sends=other.sends, complaints=other.complaints)
                for other in members
                if other.mailbox != member.mailbox
            ]
    return groups


# Defaults for the CUSUM trend check `evaluate_all_mailboxes` runs alongside
# the breaker's own posterior ladder (BUILD-PLAN.md §6). `target_rate`
# matches `engine.posterior.DEFAULT_PRIOR`'s own centre (~0.1%) -- the same
# "probably well under 1%, but we're honestly not sure yet" baseline the
# posterior prior encodes, so the two mechanisms agree on what "healthy"
# means even though they detect different things (a sustained elevated
# rate vs. a sudden shift). `slack` and `threshold` are conservative
# starting points, not a measurement against real traffic -- same caveat as
# `providers/instantly.py`'s retry defaults: replace this paragraph with
# observed behavior and the date once this runs against a live account.
DEFAULT_CUSUM_TARGET_RATE = 0.001
DEFAULT_CUSUM_SLACK = 0.0005
DEFAULT_CUSUM_THRESHOLD = 5.0


def evaluate_all_mailboxes(
    *,
    driver: ProviderDriver,
    since: date,
    prior: BetaDistribution,
    thresholds: ThresholdLadder,
    state_store: BreakerStateStore,
    dry_run: bool,
    now: datetime,
    max_pooled_ess: float = DEFAULT_MAX_POOLED_ESS,
    cusum_states: MutableMapping[MailboxRef, CusumState] | None = None,
    cusum_target_rate: float = DEFAULT_CUSUM_TARGET_RATE,
    cusum_slack: float = DEFAULT_CUSUM_SLACK,
    cusum_threshold: float = DEFAULT_CUSUM_THRESHOLD,
    on_cusum_alarm: Callable[[MailboxRef, CusumResult], None] | None = None,
) -> list[BreakerEvaluation]:
    """Pull every mailbox's stats since `since`, aggregate, and evaluate
    each one through the breaker -- building each mailbox's same-domain peer
    group for hierarchical pooling, threading through its most recently
    reported daily limit so THROTTLE can actually act, and running a CUSUM
    trend check (`engine.changepoint.cusum_step`) alongside the breaker's
    own posterior ladder (CLOSE-1).

    This is the shared pull-based evaluation path used by both `cli.cmd_check`
    (one-shot) and `loops.controller`'s fast tick (continuous polling), so
    the two cannot drift apart from each other the way a hand-duplicated
    aggregation loop in each caller eventually would -- and so both actually
    exercise pooling, throttling, and CUSUM in production rather than only
    in tests that call `engine.breaker.evaluate` directly.

    `cusum_states` is the CUSUM trend state per mailbox, carried across
    calls by the caller (mirroring `state_store`): pass the SAME mapping on
    every tick so the running statistic actually accumulates period over
    period, and `None` (the default) to skip CUSUM entirely -- a caller with
    nowhere to persist trend state between ticks is a legitimate choice, not
    an error. `on_cusum_alarm`, when given, is called once per mailbox whose
    CUSUM statistic alarms this tick; it does not itself take an action --
    that's a policy decision left to the caller, matching how a WARN verdict
    from the breaker's own ladder is notify-only too.
    """
    totals = aggregate_mailbox_stats(driver.read_mailbox_stats(since))
    peer_groups = _peer_groups(totals)
    results: list[BreakerEvaluation] = []
    for total in totals:
        results.append(
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
                current_daily_limit=total.current_daily_limit,
                peer_group=peer_groups[total.mailbox],
                max_pooled_ess=max_pooled_ess,
            )
        )
        if cusum_states is not None:
            trend = cusum_step(
                cusum_states.get(total.mailbox, CusumState()),
                total.sends,
                total.complaints,
                target_rate=cusum_target_rate,
                slack=cusum_slack,
                threshold=cusum_threshold,
            )
            cusum_states[total.mailbox] = trend.state
            if trend.alarmed and on_cusum_alarm is not None:
                on_cusum_alarm(total.mailbox, trend)
    return results
