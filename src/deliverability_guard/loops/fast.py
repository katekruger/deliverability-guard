"""Fast loop: seconds-to-minutes reaction to leading indicators.

Bounce webhooks, SMTP codes (4.7.31, 4.7.32, 5.7.515, 5.7.x), mailbox
disconnect events, and TLS delivery failures feed this loop. Complaint data
lags 24h-3 days, so the fast loop must act on what is observable now rather
than waiting on a lagging signal (BUILD-PLAN.md §5). For a 50/day mailbox
that lag is 150 messages already gone by the time a lagging signal would
have fired; for a 40-mailbox farm, 6,000.

`evaluate_all_mailboxes` below is the pull-based entry point this module
actually ships: it is the shared chokepoint `cli.cmd_check` and
`loops.controller.run`'s fast tick both call, so it is the single place
where a mailbox's peer group (for hierarchical pooling), current daily
limit (for THROTTLE), and CUSUM trend state must all be assembled and
threaded into `engine.breaker.evaluate` for either of those callers to
actually exercise `engine.posterior.pooled_posterior` or
`engine.changepoint.cusum_step` in production (CLOSE-1: before this,
`evaluate_all_mailboxes` called `evaluate()` with neither `peer_group` nor
`current_daily_limit`, so a real `check`/`run` never pooled and never
throttled).

CLOSE3-5: this module used to also define `FastLoopSignal`/`evaluate_signal`
-- per-webhook-event evaluation for a fast loop that reacts to pushed
webhooks. They moved to `experimental.webhook_signal`: nothing in this
codebase accepts an inbound webhook at all yet (see `loops.controller`'s
module docstring), so `evaluate_signal` had zero production callers. See
that module's docstring for the full reasoning and what would bring it back.

`evaluate_all_mailboxes` also gained `compliance_gate_tripped_for`
(CLOSE3-5): `signals.postmaster.forces_hard_gate` could only ever force a
PAUSE through `engine.breaker.evaluate` called directly, never through this
shared chokepoint. THE GATE ITSELF REMAINS UNWIRED TO ANY LIVE DATA SOURCE:
neither `cli.cmd_check` nor `loops.controller.run` constructs a
`PostmasterClient` or passes anything here yet -- that needs a live OAuth
token and a configured domain, real setup this PR does not add (AGENTS.md:
no new features beyond what closes the finding). What changed is that the
chokepoint can now honor a compliance signal if a caller has one, instead
of silently having nowhere to put it.
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
    compliance_gate_tripped_for: Callable[[MailboxRef], bool] | None = None,
    on_evaluation: Callable[[BreakerEvaluation], None] | None = None,
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

    `compliance_gate_tripped_for`, when given, is called once per mailbox to
    produce `engine.breaker.evaluate`'s `compliance_gate_tripped` argument
    (CLOSE3-5): before this parameter existed, `signals.postmaster.
    forces_hard_gate`'s verdict could force a PAUSE only through
    `engine.breaker.evaluate` called directly -- never through this shared
    chokepoint, so a real `check`/`run` could never actually honor Google's
    own compliance verdict regardless of what a caller had available. `None`
    (the default) reproduces the no-gate behavior every caller had before
    this parameter existed. Neither `cli.cmd_check` nor `loops.controller.
    run` passes anything here yet -- doing so needs a live Postmaster OAuth
    token and domain configured somewhere, which is real, separately-scoped
    setup (see `experimental.postmaster_coverage`'s own docstring for the
    same caveat about Postmaster ingestion generally). This closes the
    WIRING gap the chokepoint had; it is not, on its own, a live integration.

    `on_evaluation`, when given, is called with each mailbox's
    `BreakerEvaluation` immediately after `engine.breaker.evaluate` returns
    it -- BEFORE moving on to the next mailbox in `totals` (CLOSE6-1). This
    is the one place a caller can durably persist a decision record as it
    happens rather than after the whole batch: `cli.cmd_check` and
    `loops.controller.run`'s fast tick both used to evaluate every mailbox
    first and append decision records in a SEPARATE loop afterward, so a
    later mailbox's evaluation raising (e.g. a provider transport failure
    mid-`pause()` call) meant NO record was written for any mailbox in that
    tick -- including an earlier mailbox whose PAUSE had just been
    genuinely confirmed at the provider. A confirmed PAUSE with no durable
    record of it is exactly the gap ADR 0003's human-review gate exists to
    close: without one, a subsequent evaluation has no way to know the
    mailbox was ever paused, and can throttle or re-decide it as if
    nothing had happened. `on_evaluation` runs even when a LATER mailbox's
    evaluation goes on to raise, since Python evaluates loop bodies in
    order and this call happens before the loop advances.
    """
    totals = aggregate_mailbox_stats(driver.read_mailbox_stats(since))
    peer_groups = _peer_groups(totals)
    results: list[BreakerEvaluation] = []
    for total in totals:
        result = evaluate(
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
            compliance_gate_tripped=(
                compliance_gate_tripped_for(total.mailbox)
                if compliance_gate_tripped_for is not None
                else False
            ),
        )
        results.append(result)
        if on_evaluation is not None:
            on_evaluation(result)
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
