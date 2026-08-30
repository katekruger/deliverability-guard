"""The graduated response ladder: warn -> throttle -> pause.

BUILD-PLAN.md §7's default ladder, keyed on the posterior's lower confidence
bound (engine/posterior.py) -- never the point estimate:

    warn     at >= 0.05%  -> notify only
    throttle at >= 0.10%  -> reduce daily limit 50%
    pause    at >= 0.20%  -> pause the mailbox

Why these specific numbers, and why they sit below Google's published 0.3%
ceiling: 0.3% is a TERMINAL threshold -- by the time a breaker's evidence is
strong enough to be confident the true rate has crossed it, the damage is
already done, because complaint data itself lags 24h-3 days behind send
(BUILD-PLAN.md §5). Waiting for 0.3% to trip is building a post-mortem
generator, not a breaker. 0.10% is the natural amber line instead: it is
simultaneously Google's own RECOMMENDED target rate and Amazon SES's own
review-trigger threshold -- two independent providers converging on the same
number as "you should be worried now," which is why it sits mid-ladder
rather than at the top.

IMPORTANT, and this belongs in the README too: Google's 0.3% and SES's 0.1%
are NOT the same measurement and must never be blended into one number.
Google's denominator is Gmail inbox-delivered, DKIM-authenticated mail to
engaged users. SES's is mail to domains that return complaint feedback to
SES. This ladder's thresholds are provider-agnostic POLICY points chosen
with both in mind, not a claim that the two rates are directly comparable.

Dry-run is the default everywhere (AGENTS.md). It is implemented as a no-op
provider decorator (providers/dry_run.py) wrapped around whatever real
driver is passed in -- never as a separate branch of logic in this module.
`evaluate()` below runs the exact same code whether dry_run is True or
False; only the driver object passed to the provider call differs.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from deliverability_guard.engine.posterior import (
    DEFAULT_MAX_POOLED_ESS,
    BetaDistribution,
    GroupObservation,
    pooled_posterior,
    update,
)
from deliverability_guard.engine.state import DataState
from deliverability_guard.providers.base import (
    ActionOutcome,
    ActionResult,
    Capability,
    MailboxRef,
    ProviderDriver,
)
from deliverability_guard.providers.dry_run import DryRunDriver


@dataclass(frozen=True, slots=True)
class ThresholdLadder:
    warn: float
    throttle: float
    pause: float

    def __post_init__(self) -> None:
        if not (0 <= self.warn <= self.throttle <= self.pause):
            raise ValueError(
                "thresholds must satisfy 0 <= warn <= throttle <= pause, got "
                f"warn={self.warn}, throttle={self.throttle}, pause={self.pause}"
            )


# See module docstring for the full rationale behind these three numbers.
DEFAULT_LADDER = ThresholdLadder(warn=0.0005, throttle=0.0010, pause=0.0020)


class ThresholdStore:
    """Holds the current ThresholdLadder, swapped atomically on reload.

    A config reload while the breaker is running must never let an
    in-progress evaluation observe a half-updated ladder (BUILD-PLAN.md §5's
    "torn read" edge case). `ThresholdLadder` is frozen, and `swap` replaces
    the reference to it wholesale rather than mutating any of its fields --
    there is no partially-updated object for a concurrent reader to ever
    observe, by construction, not by locking discipline that has to be
    remembered at every call site.
    """

    def __init__(self, initial: ThresholdLadder) -> None:
        self._current = initial

    @property
    def current(self) -> ThresholdLadder:
        return self._current

    def swap(self, new: ThresholdLadder) -> None:
        self._current = new


class Verdict(Enum):
    OK = auto()
    WARN = auto()
    THROTTLE = auto()
    PAUSE = auto()


def rung(lower_bound: float, thresholds: ThresholdLadder) -> Verdict:
    """Which ladder rung a posterior lower bound reaches, evaluated highest-first."""
    if lower_bound >= thresholds.pause:
        return Verdict.PAUSE
    if lower_bound >= thresholds.throttle:
        return Verdict.THROTTLE
    if lower_bound >= thresholds.warn:
        return Verdict.WARN
    return Verdict.OK


class MailboxBreakerStatus(Enum):
    ACTIVE = auto()
    THROTTLED = auto()
    PAUSE_IN_FLIGHT = auto()
    PAUSED = auto()


class BreakerStateStore:
    """Tracks per-mailbox pause/throttle status, in memory, injectable for
    tests.

    This is what makes a repeat PAUSE verdict idempotent (BUILD-PLAN.md §5:
    "breaker trips while a previous trip is still in flight -> idempotent,
    no double-pause"), what makes a repeat THROTTLE verdict idempotent
    (ENG-5a: six identical THROTTLE evaluations must not compound into
    50 -> 25 -> 12 -> 6 -> 3 -> 1), and what lets a lost provider response
    get reconciled on the next tick instead of either silently giving up or
    blindly assuming success.

    `resume_after_human_review` is the ONLY path back from PAUSED to ACTIVE
    in this entire class. Nothing in `evaluate()` below calls it. See ADR
    0003: never auto-resume a paused mailbox.
    """

    def __init__(self) -> None:
        self._status: dict[MailboxRef, MailboxBreakerStatus] = {}

    def status_of(self, mailbox: MailboxRef) -> MailboxBreakerStatus:
        return self._status.get(mailbox, MailboxBreakerStatus.ACTIVE)

    def mark_throttled(self, mailbox: MailboxRef) -> None:
        self._status[mailbox] = MailboxBreakerStatus.THROTTLED

    def mark_pause_in_flight(self, mailbox: MailboxRef) -> None:
        self._status[mailbox] = MailboxBreakerStatus.PAUSE_IN_FLIGHT

    def mark_paused(self, mailbox: MailboxRef) -> None:
        self._status[mailbox] = MailboxBreakerStatus.PAUSED

    def mark_active(self, mailbox: MailboxRef) -> None:
        """For a pause attempt that definitively failed (provider said no),
        not for "response lost" (which must stay PAUSE_IN_FLIGHT so the next
        tick reconciles) and never for un-pausing a confirmed-paused mailbox."""
        self._status[mailbox] = MailboxBreakerStatus.ACTIVE

    def resume_after_human_review(self, mailbox: MailboxRef) -> None:
        """The only way back from PAUSED to ACTIVE. Not called anywhere in
        this module's automatic evaluation path. See ADR 0003."""
        self._status[mailbox] = MailboxBreakerStatus.ACTIVE


# A mailbox throttled all the way to 0/day is a pause wearing a different
# hat -- it blurs the ladder's rungs into each other. 1 is the floor.
_MIN_THROTTLED_DAILY_LIMIT = 1


@dataclass(frozen=True, slots=True)
class BreakerEvaluation:
    """Everything one evaluation decided, and everything a decision-log
    record (audit/log.py) needs to reproduce it later. `evaluated_at` is
    local wall-clock time, injected by the caller rather than read from the
    system clock in here -- both for determinism in tests and because it is
    NOT the same timestamp as whatever the provider itself reports for the
    underlying data (BUILD-PLAN.md §5's clock-skew edge case: log both,
    which is why `sends`/`complaints` here are paired with `evaluated_at`
    while the provider's own reporting timestamp lives on the
    MailboxDayStats the caller read them from, not duplicated here).
    """

    evaluated_at: datetime
    mailbox: MailboxRef
    sends: int
    complaints: int
    data_state: DataState
    prior: BetaDistribution
    posterior: BetaDistribution | None
    lower_bound: float | None
    confidence: float
    thresholds: ThresholdLadder
    verdict: Verdict
    dry_run: bool
    action: ActionResult | None


def evaluate(
    *,
    driver: ProviderDriver,
    mailbox: MailboxRef,
    sends: int,
    complaints: int,
    prior: BetaDistribution,
    thresholds: ThresholdLadder,
    state_store: BreakerStateStore,
    dry_run: bool,
    now: datetime,
    confidence: float = 0.95,
    current_daily_limit: int | None = None,
    compliance_gate_tripped: bool = False,
    peer_group: Iterable[GroupObservation] | None = None,
    max_pooled_ess: float = DEFAULT_MAX_POOLED_ESS,
) -> BreakerEvaluation:
    """Evaluate one mailbox and, if warranted, attempt an action.

    `peer_group` is the mailbox's OTHER same-domain (or same-tenant) members
    -- leave-one-out, matching `engine.posterior.pooled_posterior`'s own
    contract. When given, the posterior is computed via hierarchical partial
    pooling instead of this mailbox's own data alone: a mailbox with little
    of its own evidence borrows strength from a healthy or unhealthy peer
    group, capped at `max_pooled_ess` so a large peer group can never dilute
    a mailbox with enough of its own evidence into looking healthy (ADR
    0002). `None` (the default) reproduces the flat, non-pooled posterior
    every caller used before this parameter existed.

    `thresholds` must be a value already snapshotted by the caller (e.g.
    `ThresholdStore.current` read once before calling) -- this function
    never re-reads a mutable config source mid-evaluation, which is what
    keeps a threshold changed by the slow loop mid-evaluation from ever
    producing an inconsistent decision (BUILD-PLAN.md §5's snapshot-at-start
    edge case).

    `dry_run` has no default: every call site must decide explicitly.
    AGENTS.md: no code path may pause or throttle a real mailbox without
    `dry_run=False` set on purpose.

    `compliance_gate_tripped` is the hard gate from
    `signals.postmaster.forces_hard_gate` (BUILD-PLAN.md §5): if Google's
    own `getComplianceStatus` says a domain's deliverability verdict needs
    work, that outranks any statistical inference from this function's own
    posterior and forces PAUSE regardless of volume -- including when
    `sends == 0`, since the compliance verdict is an account-level fact
    Google is asserting independent of today's send volume, not something
    derived from today's data at all.
    """
    if sends < 0:
        raise ValueError(f"sends must be >= 0, got {sends}")
    if not 0 <= complaints <= sends:
        raise ValueError(f"complaints must be between 0 and sends ({sends}), got {complaints}")

    if compliance_gate_tripped:
        posterior = (
            _posterior_for(prior, sends, complaints, peer_group, max_pooled_ess)
            if sends > 0
            else None
        )
        lower_bound = posterior.lower_bound(confidence) if posterior is not None else None
        action = _act(
            driver,
            mailbox,
            Verdict.PAUSE,
            state_store,
            dry_run=dry_run,
            current_daily_limit=current_daily_limit,
        )
        return BreakerEvaluation(
            evaluated_at=now,
            mailbox=mailbox,
            sends=sends,
            complaints=complaints,
            data_state=DataState.OK if sends > 0 else DataState.INSUFFICIENT_DATA,
            prior=prior,
            posterior=posterior,
            lower_bound=lower_bound,
            confidence=confidence,
            thresholds=thresholds,
            verdict=Verdict.PAUSE,
            dry_run=dry_run,
            action=action,
        )

    if sends == 0:
        return BreakerEvaluation(
            evaluated_at=now,
            mailbox=mailbox,
            sends=sends,
            complaints=complaints,
            data_state=DataState.INSUFFICIENT_DATA,
            prior=prior,
            posterior=None,
            lower_bound=None,
            confidence=confidence,
            thresholds=thresholds,
            verdict=Verdict.OK,
            dry_run=dry_run,
            action=None,
        )

    posterior = _posterior_for(prior, sends, complaints, peer_group, max_pooled_ess)
    lower_bound = posterior.lower_bound(confidence)
    verdict = rung(lower_bound, thresholds)

    if (
        verdict is Verdict.THROTTLE
        and current_daily_limit is not None
        and current_daily_limit // 2 < _MIN_THROTTLED_DAILY_LIMIT
    ):
        # Escalate: a throttle that would floor-clamp is a pause wearing a
        # different hat (see `_MIN_THROTTLED_DAILY_LIMIT`'s docstring). Route
        # it through PAUSE -- and therefore through the human-review gate
        # (ADR 0003) -- instead of silently clamping at the floor forever.
        verdict = Verdict.PAUSE

    action: ActionResult | None = None
    if verdict in (Verdict.THROTTLE, Verdict.PAUSE):
        action = _act(
            driver,
            mailbox,
            verdict,
            state_store,
            dry_run=dry_run,
            current_daily_limit=current_daily_limit,
        )

    return BreakerEvaluation(
        evaluated_at=now,
        mailbox=mailbox,
        sends=sends,
        complaints=complaints,
        data_state=DataState.OK,
        prior=prior,
        posterior=posterior,
        lower_bound=lower_bound,
        confidence=confidence,
        thresholds=thresholds,
        verdict=verdict,
        dry_run=dry_run,
        action=action,
    )


def _posterior_for(
    prior: BetaDistribution,
    sends: int,
    complaints: int,
    peer_group: Iterable[GroupObservation] | None,
    max_pooled_ess: float,
) -> BetaDistribution:
    """Flat `update()` when there's no peer group; hierarchical partial
    pooling (`engine.posterior.pooled_posterior`) when there is. See
    `evaluate`'s `peer_group` parameter and ADR 0002."""
    if peer_group is None:
        return update(prior, sends, complaints)
    return pooled_posterior(
        prior, peer_group, own_sends=sends, own_complaints=complaints, max_ess=max_pooled_ess
    )


def _act(
    driver: ProviderDriver,
    mailbox: MailboxRef,
    verdict: Verdict,
    state_store: BreakerStateStore,
    *,
    dry_run: bool,
    current_daily_limit: int | None,
) -> ActionResult:
    effective_driver: ProviderDriver = DryRunDriver(inner=driver) if dry_run else driver

    if verdict is Verdict.THROTTLE:
        if state_store.status_of(mailbox) is MailboxBreakerStatus.THROTTLED:
            # Idempotent, keyed on the VERDICT, not the limit (ENG-5a): a
            # mailbox already throttled for this same verdict is a no-op --
            # otherwise every repeat evaluation halves the limit again
            # (50 -> 25 -> 12 -> ...) until it silently becomes a pause with
            # no human ever seeing it happen.
            return ActionResult(
                outcome=ActionOutcome.PERFORMED,
                detail="mailbox already throttled at this verdict; no action taken (idempotent)",
                capability=Capability.THROTTLE,
            )
        if current_daily_limit is None:
            return ActionResult(
                outcome=ActionOutcome.UNSUPPORTED,
                detail=(
                    "throttle verdict reached but the mailbox's current daily "
                    "limit is unknown, so a 50% reduction cannot be computed"
                ),
                capability=Capability.THROTTLE,
            )
        new_limit = max(_MIN_THROTTLED_DAILY_LIMIT, current_daily_limit // 2)
        result = effective_driver.throttle(mailbox.mailbox_id, new_limit)
        if result.outcome is ActionOutcome.PERFORMED:
            state_store.mark_throttled(mailbox)
        return result

    # PAUSE.
    status = state_store.status_of(mailbox)
    if status is MailboxBreakerStatus.PAUSED:
        # Idempotent: a previous trip already got this mailbox paused (and
        # confirmed). Don't call pause() again.
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail="mailbox already paused; no action taken (idempotent)",
            capability=Capability.PAUSE,
        )

    # ACTIVE or PAUSE_IN_FLIGHT: attempt (or re-attempt) the pause. Real
    # pause endpoints are themselves idempotent server-side (pausing an
    # already-paused mailbox is a no-op), so re-attempting when we genuinely
    # don't know whether the last attempt landed IS the "reconcile on next
    # tick" behavior this is meant to provide -- not a double-pause.
    state_store.mark_pause_in_flight(mailbox)
    result = effective_driver.pause(mailbox)
    match result.outcome:
        case ActionOutcome.PERFORMED:
            state_store.mark_paused(mailbox)
        case ActionOutcome.UNSUPPORTED | ActionOutcome.FAILED:
            # A clean, definitive answer either way -- "this provider will
            # never support pausing this target" or "the provider
            # explicitly said no" -- so there's nothing ambiguous left to
            # reconcile.
            state_store.mark_active(mailbox)
        case _:  # pragma: no cover
            raise AssertionError(f"unreachable: unhandled ActionOutcome {result.outcome!r}")
    # If the call raises instead of returning (e.g. RateLimitExceededError,
    # or any transport-level failure), none of the branches above run and
    # status stays PAUSE_IN_FLIGHT: that's the genuinely ambiguous case --
    # we don't know whether the provider received and acted on the request
    # -- so we don't guess. The exception propagates to the caller
    # unchanged; this function never swallows a provider error, and the
    # next tick's re-attempt is the reconciliation.
    return result
