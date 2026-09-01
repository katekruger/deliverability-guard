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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

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
    # A pause attempt got a definitive FAILED from the provider. Distinct
    # from ACTIVE (CLOSE-3c): ACTIVE means "verified healthy," and treating a
    # failed pause as ACTIVE let a subsequent THROTTLE verdict see a mailbox
    # that still needed a real limit reduction as if it were pristine,
    # re-halving an already-throttled limit (25 -> 12) instead of staying
    # idempotent against what was already applied. See `_act`'s PAUSE branch
    # and `BreakerStateStore.mark_pause_failed`.
    PAUSE_FAILED = auto()


class BreakerStateStoreLoadError(Exception):
    """The persisted decision log exists but couldn't be read or parsed.

    Raised instead of silently falling back to an empty (every-mailbox-
    ACTIVE) store: `status_of` defaulting to ACTIVE for a mailbox with no
    record is correct for a genuinely new mailbox, but wrong for one whose
    state failed to load -- that could auto-resume a mailbox that was
    actually PAUSED, which is exactly what ADR 0003 exists to prevent. See
    `BreakerStateStore.from_log`.
    """


class BreakerStateStore:
    """Tracks per-mailbox pause/throttle status, in memory by default, and
    rebuildable from `audit.log`'s append-only decision log via `from_log`
    so a process restart doesn't silently un-pause every paused mailbox
    (ENG-5b: at HEAD, `_status` was in-memory only, and restarting the
    process reset every mailbox to ACTIVE regardless of pause history).

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

    def __init__(
        self,
        initial: Mapping[MailboxRef, MailboxBreakerStatus] | None = None,
        initial_throttled_at_limit: Mapping[MailboxRef, int] | None = None,
        initial_unsupported_throttle_streak: Mapping[MailboxRef, int] | None = None,
    ) -> None:
        self._status: dict[MailboxRef, MailboxBreakerStatus] = dict(initial) if initial else {}
        # The `current_daily_limit` INPUT that was in force the last time
        # this mailbox was actually throttled (not the halved result) --
        # CLOSE-3b's "(verdict, applied limit)" idempotency key. A THROTTLE
        # verdict is only a repeat, not a new event, when the mailbox's
        # current limit hasn't grown past this since. Deliberately survives
        # a PAUSE attempt that FAILS (see `mark_pause_failed`): CLOSE-3c's
        # bug was exactly that a failed pause erased this memory, letting
        # the next THROTTLE re-halve an already-throttled limit.
        self._throttled_at_limit: dict[MailboxRef, int] = (
            dict(initial_throttled_at_limit) if initial_throttled_at_limit else {}
        )
        # Consecutive THROTTLE evaluations in a row whose action came back
        # UNSUPPORTED -- the provider can't execute it, either because
        # `current_daily_limit` is unknown or because the driver structurally
        # can't throttle at all (CLOSE3-2). Reset by any other outcome; once
        # it reaches `_MAX_UNSUPPORTED_THROTTLE_STREAK`, `evaluate()`
        # escalates the verdict to PAUSE instead of writing another
        # identical UNSUPPORTED record forever.
        self._unsupported_throttle_streak: dict[MailboxRef, int] = (
            dict(initial_unsupported_throttle_streak) if initial_unsupported_throttle_streak else {}
        )

    @classmethod
    def from_log(cls, path: Path) -> "BreakerStateStore":
        """Rebuild pause/throttle state by replaying `audit.log`'s decision
        records in order. NOT simple most-recent-wins (CLOSE4-1): the
        governing rule is that a record whose action did not actually touch
        the real provider must not change state either, exactly mirroring
        `_act`'s own live-path behavior for that same outcome. Getting this
        wrong is subtle -- three of `_act`'s branches (THROTTLE/UNSUPPORTED,
        THROTTLE/FAILED, and a THROTTLE/PERFORMED that was actually `_act`'s
        own idempotent no-op) never touch `state_store` on the live path,
        and replaying any of them as if they had un-paused a mailbox a
        `PAUSE`/`PERFORMED` record earlier in the log had put behind the
        human-review gate (ADR 0003). `tests/test_breaker.py`'s
        `test_from_log_replay_matches_the_live_path_over_every_move_ordering`
        checks this invariant directly, over every ordering (now every
        `itertools.product` combination, so repeated moves are covered too)
        of a small move set, rather than only the specific sequences that
        have already been found broken.

        CLOSE5-1 added a fourth case the live path itself was missing: a
        PAUSED mailbox's THROTTLE verdict now short-circuits in `_act` too
        (mirroring the PAUSE branch's own already-PAUSED check), so replay
        had to learn a THROTTLE/PERFORMED record can ALSO mean "this
        mailbox was already paused," not just "already throttled at this
        limit." CLOSE5-2 found the inverse mistake in the SAME idea applied
        wrong: PAUSE's `UNSUPPORTED` branch DOES touch `state_store` on the
        live path (`mark_active`, called deliberately, not skipped) -- but
        replay only mirrored HALF of what `mark_active` does (status, not
        `throttled_at_limit`), which is a different bug from "should not
        have changed state at all."

        CLOSE6-2 added a guard the PAUSE `UNSUPPORTED`/`FAILED` branches
        never had: once a mailbox's status entering one of those records is
        already PAUSED, that record is left with no effect, mirroring
        `_act`'s own already-PAUSED short-circuit -- `_act`'s PAUSE branch
        can never itself PRODUCE a FAILED or UNSUPPORTED outcome for an
        already-PAUSED mailbox, so a record claiming one can only come from
        a log that's been hand-edited, merged, or restored from a stale
        backup. Before this guard, replaying such a record silently
        un-paused a mailbox behind ADR 0003's human-review gate with no
        `ResumeRecord` anywhere in the log -- not reachable through the
        live path this repo's own CLI can produce, but very much something
        an operator inspecting or repairing a JSONL file by hand could
        produce.

        `path` not existing at all means there is genuinely no history yet
        (a brand-new deployment) -- returns an empty store, where every
        mailbox correctly defaults to ACTIVE. `path` existing but failing to
        read or parse is a DIFFERENT situation and fails loudly with
        `BreakerStateStoreLoadError` rather than returning that same empty
        store, which would be indistinguishable from "no history" and could
        silently un-pause a mailbox that the log actually says is PAUSED.

        `resume_after_human_review` now writes its own `ResumeRecord` to the
        same log (see `cli.cmd_resume`), replayed here in file order
        alongside decision records -- a mailbox resumed by a human and never
        evaluated again before a restart correctly rebuilds as ACTIVE.

        A record whose `dry_run` is `True` is skipped entirely when deriving
        status: a dry-run action never touched the real provider (AGENTS.md:
        dry-run must be a no-op), so it must never be read back as having
        actually paused or throttled a mailbox -- that would let a dry-run
        deployment accumulate persistent PAUSED state for mailboxes it was
        explicitly configured never to touch, exactly the failure the
        default-dry-run non-negotiable exists to prevent.

        An existing-but-empty log file is treated the same as an unreadable
        one, raising `BreakerStateStoreLoadError` rather than silently
        producing the empty (every-mailbox-ACTIVE) store: a zero-byte log is
        indistinguishable from "the log was truncated mid-write" and could
        just as easily mean history was lost as mean nothing was ever
        written. Only a log path that doesn't exist AT ALL is genuinely "no
        history yet" (see the `not path.exists()` branch above).

            A rebuilt THROTTLED mailbox's `throttled_at_limit` memo (CLOSE-3b's
        idempotency key) IS restored, from `DecisionRecord.applied_daily_limit`
        (CLOSE3-1): every `DecisionRecord` for a PERFORMED throttle now
        persists the `current_daily_limit` input the halving was computed
        against, so a process that restarts between every single evaluation
        -- e.g. `deliverability-guard check` run from cron, where every
        invocation genuinely is a fresh process -- stays idempotent from the
        very first re-evaluation after the first throttle, instead of
        re-acting once per restart. Before this, six separate `check`
        invocations against one mailbox compounded 50 -> 25 -> 12 -> 6 -> 3
        -> an unearned PAUSE, because every invocation's `from_log` forgot
        the limit it had just applied.

        An OK verdict recorded after a THROTTLE is also honoured here as a
        recovery (CLOSE3-3), mirroring `evaluate()`'s own
        `state_store.mark_active` call on sustained recovery: a mailbox
        rebuilt from a log ending in a healthy evaluation comes back ACTIVE,
        with `throttled_at_limit` cleared, instead of reading THROTTLED
        forever just because the process restarted before `evaluate()`
        itself ever saw the recovery.

        CLOSE7-1: that recovery check used to trust `Verdict.OK` alone,
        never `record.data_state` -- so a zero-send day (`evaluate()`'s
        `sends == 0` early return, always `Verdict.OK` +
        `DataState.INSUFFICIENT_DATA`, never an action of any kind) replayed
        as CLOSE3-3's sustained recovery: absence of evidence collapsed into
        "the mailbox got better." `engine/state.py`'s own module docstring
        is why `DataState` exists at all -- a throttled mailbox sending less
        BECAUSE it was throttled can drop out of a provider's reporting
        entirely, and that silence must never read as OK. A dedicated
        elif branch now intercepts every `Verdict.OK` + `DataState.
        INSUFFICIENT_DATA` record before the recovery check ever sees it,
        leaving status, `throttled_at_limit`, and the unsupported-throttle
        streak all untouched -- exactly the no-op `evaluate()`'s own early
        return is.

        Every OTHER branch was checked against this same question --
        "does `evaluate()` ever pair this verdict/action_outcome with
        `INSUFFICIENT_DATA`, and if so does this branch already do the
        right thing?" -- while fixing this. The answer for all of them is
        yes, already correct, and for the same reason: `evaluate()` can
        only ever produce `INSUFFICIENT_DATA` two ways -- the `sends == 0`
        early return just described (always `Verdict.OK`, always
        `action=None`), or `compliance_gate_tripped` with `sends == 0`
        (always `Verdict.PAUSE`, with a REAL `action` -- the compliance gate
        overrides volume, not the other way around). The PAUSE branch above
        already keys entirely off `action_outcome`, never `data_state`, so a
        compliance-forced PAUSE on zero sends replays correctly with no
        change needed -- `data_state` never distinguishes a real action from
        a fake one there, because `INSUFFICIENT_DATA` paired with PAUSE
        always means a real `_act` call happened. THROTTLE can never pair
        with `INSUFFICIENT_DATA` at all (THROTTLE requires `sends > 0` to
        even compute a verdict past the ladder). The only place
        `INSUFFICIENT_DATA` could ever masquerade as evidence was the
        OK-verdict recovery check, which is why that is the only branch
        that needed to change.
        """
        # Imported here, not at module level, to avoid a circular import:
        # audit.log imports BreakerEvaluation/ThresholdLadder/Verdict/rung
        # from this module.
        import json

        from deliverability_guard.audit.log import (
            CorruptDecisionRecordError,
            ResumeRecord,
            read_events,
        )

        if not path.exists():
            return cls()
        try:
            if path.stat().st_size == 0:
                raise BreakerStateStoreLoadError(
                    f"decision log {path} exists but is empty -- ambiguous between "
                    "'nothing has ever been recorded' and 'the log was truncated', "
                    "so refusing to rebuild an all-ACTIVE store from it"
                )
            events = read_events(path)
        except (OSError, json.JSONDecodeError, CorruptDecisionRecordError) as exc:
            raise BreakerStateStoreLoadError(
                f"could not rebuild breaker state from {path}: {exc}"
            ) from exc

        status: dict[MailboxRef, MailboxBreakerStatus] = {}
        throttled_at_limit: dict[MailboxRef, int] = {}
        unsupported_streak: dict[MailboxRef, int] = {}
        for event in events:
            if isinstance(event, ResumeRecord):
                mailbox = MailboxRef(provider=event.provider, mailbox_id=event.mailbox_id)
                status[mailbox] = MailboxBreakerStatus.ACTIVE
                throttled_at_limit.pop(mailbox, None)
                unsupported_streak.pop(mailbox, None)
                continue
            record = event
            if record.dry_run:
                # Never actually touched the provider -- see docstring above.
                continue
            mailbox = MailboxRef(provider=record.provider, mailbox_id=record.mailbox_id)
            if record.verdict is Verdict.PAUSE:
                if record.action_outcome is ActionOutcome.PERFORMED:
                    status[mailbox] = MailboxBreakerStatus.PAUSED
                elif status.get(mailbox) is MailboxBreakerStatus.PAUSED:
                    # CLOSE6-2: `_act`'s PAUSE branch short-circuits to an
                    # idempotent PERFORMED result the instant a mailbox is
                    # already PAUSED -- it never calls `driver.pause()`
                    # again, so it can never itself produce a FAILED or
                    # UNSUPPORTED PAUSE record for a mailbox that's already
                    # PAUSED. The FAILED and UNSUPPORTED branches just below
                    # therefore have no live-path analogue to mirror once a
                    # PAUSE/PERFORMED record for this mailbox has already
                    # been replayed -- unlike the THROTTLE/PERFORMED branch
                    # below, which DOES have to handle exactly this
                    # already-PAUSED case, because `_act`'s THROTTLE branch
                    # (CLOSE5-1) can genuinely still emit one. A log
                    # containing such a record here can only be hand-edited,
                    # merged, or restored from a stale backup -- and
                    # replaying it must not silently un-pause a mailbox
                    # behind ADR 0003's human-review gate just because the
                    # record's own outcome claims otherwise. Leave status
                    # (and `throttled_at_limit`) exactly as they are.
                    pass
                elif record.action_outcome is ActionOutcome.FAILED:
                    # Mirrors `_act`'s live-path distinction (CLOSE-3c): a
                    # FAILED pause is not "verified healthy."
                    status[mailbox] = MailboxBreakerStatus.PAUSE_FAILED
                else:
                    # UNSUPPORTED. Mirrors `_act`'s live-path behavior:
                    # `_act`'s PAUSE branch calls `state_store.mark_active`
                    # unconditionally for a definitively-UNSUPPORTED pause
                    # ("no reason to treat this mailbox as anything but
                    # pristine going forward") -- and `mark_active` clears
                    # BOTH `_throttled_at_limit` and the unsupported-throttle
                    # streak (CLOSE5-2). This branch used to set status
                    # without clearing `throttled_at_limit`, leaving a stale
                    # value behind that made the very next THROTTLE/
                    # PERFORMED record look like CLOSE4-1's
                    # `is_idempotent_replay` case, so replay never restored
                    # THROTTLED. Reachable in production on `smartlead`
                    # (`pause(MailboxRef)` UNSUPPORTED,
                    # `throttle(mailbox_id, limit)` PERFORMED): on identical
                    # evidence, `run` (no restart) and `check` (restart
                    # between every evaluation) made a DIFFERENT number of
                    # real provider calls, and after any restart the breaker
                    # read a genuinely throttled mailbox as pristine.
                    #
                    # NOTE this is unrelated to the status question CLOSE5-1
                    # closed: `_act`'s PAUSE branch is still structurally
                    # unreachable from an already-PAUSED mailbox (it
                    # short-circuits to an idempotent PERFORMED result
                    # before ever calling the driver in that case), so this
                    # branch genuinely can still run against a mailbox that
                    # was never PAUSED at all -- CLOSE5-2's reproduction
                    # never starts from PAUSED. The status assignment below
                    # is therefore still correct as written; only the limit
                    # was ever wrong.
                    status[mailbox] = MailboxBreakerStatus.ACTIVE
                    throttled_at_limit.pop(mailbox, None)
                unsupported_streak.pop(mailbox, None)
            elif record.verdict is Verdict.THROTTLE:
                if record.action_outcome is ActionOutcome.PERFORMED:
                    # A PERFORMED throttle can itself be `_act`'s idempotent
                    # no-op, in TWO different ways: "mailbox already
                    # throttled at this limit" (CLOSE4-1), or -- CLOSE5-1 --
                    # "mailbox is paused; throttle refused pending human
                    # review." Neither ever touches `state_store` on the
                    # live path, so neither must overwrite a PAUSED status
                    # here either.
                    #
                    # If the mailbox's status entering this record is
                    # already PAUSED, this record can ONLY be CLOSE5-1's
                    # paused-idempotent no-op: `_act`'s THROTTLE branch now
                    # checks PAUSED status before anything else, so a
                    # genuine throttle can never be recorded for a mailbox
                    # that was already PAUSED at the time -- leave status
                    # (and `throttled_at_limit`) exactly as they were.
                    #
                    # Otherwise, CLOSE4-1's original distinction still
                    # applies: `_act` only reaches its limit-idempotent
                    # branch when a limit is already recorded and this
                    # record's `applied_daily_limit` doesn't exceed it
                    # (mirroring the live check `current_daily_limit <=
                    # recorded_limit`) -- a genuine throttle always either
                    # sets the limit for the first time (nothing recorded
                    # yet) or records a STRICTLY larger one.
                    if status.get(mailbox) is MailboxBreakerStatus.PAUSED:
                        is_idempotent_replay = True
                    else:
                        previously_recorded_limit = throttled_at_limit.get(mailbox)
                        is_idempotent_replay = (
                            previously_recorded_limit is not None
                            and record.applied_daily_limit is not None
                            and record.applied_daily_limit <= previously_recorded_limit
                        )
                    if not is_idempotent_replay:
                        status[mailbox] = MailboxBreakerStatus.THROTTLED
                        if record.applied_daily_limit is not None:
                            throttled_at_limit[mailbox] = record.applied_daily_limit
                    unsupported_streak.pop(mailbox, None)
                elif record.action_outcome is ActionOutcome.UNSUPPORTED:
                    # CLOSE4-1: unlike PAUSE's UNSUPPORTED branch above,
                    # `_act`'s THROTTLE branch does NOT call `mark_active` --
                    # or touch `state_store` at all -- when the outcome is
                    # UNSUPPORTED (missing `current_daily_limit`, or a
                    # driver with no throttle primitive). A record that did
                    # not act must not change status or `throttled_at_limit`
                    # here either: this used to unconditionally reset both
                    # to ACTIVE/cleared, silently un-pausing a mailbox that
                    # a PAUSE/PERFORMED record earlier in the log had put
                    # behind the human-review gate (ADR 0003). Only the
                    # streak of consecutive UNSUPPORTED throttles (CLOSE3-2)
                    # is genuinely tracked outside `state_store`'s
                    # status/limit, so only it changes here.
                    unsupported_streak[mailbox] = unsupported_streak.get(mailbox, 0) + 1
                else:  # FAILED
                    # Same reasoning as UNSUPPORTED just above: `_act` only
                    # calls `mark_throttled` on a PERFORMED outcome, so a
                    # FAILED throttle leaves status/throttled_at_limit alone
                    # on the live path too. The streak DOES reset here,
                    # mirroring `evaluate()`'s own
                    # `clear_unsupported_throttle_streak` call for a FAILED
                    # throttle outcome.
                    unsupported_streak.pop(mailbox, None)
            elif record.verdict is Verdict.OK and record.data_state is DataState.INSUFFICIENT_DATA:
                # CLOSE7-1: a zero-send day. `evaluate()`'s own `sends == 0`
                # early return takes NO action of any kind -- it returns
                # BEFORE reaching any `state_store` mutation, including the
                # "verdict is not THROTTLE -> clear the unsupported-throttle
                # streak" line every other non-THROTTLE verdict hits. Replay
                # must leave status, `throttled_at_limit`, AND the streak
                # untouched here too -- not just status, which is all the
                # neighbouring recovery branch below used to guard against.
                #
                # This verdict/data_state combination is unambiguous: the
                # ONLY way `evaluate()` ever returns `Verdict.OK` paired with
                # `DataState.INSUFFICIENT_DATA` is the `sends == 0` early
                # return itself (the OTHER path that can produce
                # `INSUFFICIENT_DATA`, `compliance_gate_tripped`, always
                # forces `Verdict.PAUSE`, never `OK` -- see `evaluate`'s own
                # branch above -- so it's already handled correctly by the
                # PAUSE branch above, unaffected by `data_state`).
                #
                # `engine/state.py`'s own module docstring is the reason
                # this matters: a throttled mailbox sends less as a DIRECT
                # RESULT of being throttled, can drop below a provider's
                # reporting threshold, and disappear from stats entirely --
                # `DataState.INSUFFICIENT_DATA` exists precisely so that
                # silence is never read back as "it got better." Before this
                # branch existed, the recovery branch below fired on verdict
                # alone: a zero-send day cleared THROTTLED to ACTIVE and
                # erased `throttled_at_limit`, so the very next bad day
                # re-throttled from the mailbox's ORIGINAL limit instead of
                # halving further -- CLOSE3-1's exact compounding failure
                # ("50 -> 25 -> 12 -> 6 -> 3 -> an unearned PAUSE"),
                # resurfacing through a door the permutation sweep couldn't
                # see until `ZERO_SENDS` was added to its move set.
                pass
            elif (
                record.verdict is Verdict.OK
                and status.get(mailbox) is MailboxBreakerStatus.THROTTLED
            ):
                # CLOSE3-3: mirrors `evaluate()`'s own sustained-recovery
                # check (`state_store.mark_active` on a THROTTLED mailbox
                # seeing an OK verdict) -- an OK verdict recorded after a
                # THROTTLE means the mailbox recovered. Without this, a
                # rebuild from the log left a fully recovered mailbox
                # reading THROTTLED forever, purely because the process
                # happened to restart before `evaluate()` itself ever saw
                # the OK verdicts in sequence. Reaching this branch at all
                # means `data_state is DataState.OK` (the elif just above
                # already caught the only other possibility for a `Verdict.
                # OK` record), so this really is evidence, not silence.
                status[mailbox] = MailboxBreakerStatus.ACTIVE
                throttled_at_limit.pop(mailbox, None)
                unsupported_streak.pop(mailbox, None)
            else:
                # WARN, or an OK that isn't a recovery: notify-only or
                # nothing happened for status/limit -- leave whatever status
                # this mailbox already had alone (a PAUSED mailbox reporting
                # healthy-looking evidence later stays PAUSED; only
                # `resume_after_human_review`, via a `ResumeRecord` above,
                # can change that). The unsupported-throttle streak DOES
                # still reset here, mirroring `evaluate()`'s own
                # `verdict is not THROTTLE -> clear` rule.
                unsupported_streak.pop(mailbox, None)
        return cls(status, throttled_at_limit, unsupported_streak)

    def status_of(self, mailbox: MailboxRef) -> MailboxBreakerStatus:
        return self._status.get(mailbox, MailboxBreakerStatus.ACTIVE)

    def throttled_at_limit(self, mailbox: MailboxRef) -> int | None:
        """The `current_daily_limit` input in force the last time this
        mailbox was actually throttled, or `None` if it never has been (or
        that memory has since been cleared by genuine recovery -- see
        `mark_active`)."""
        return self._throttled_at_limit.get(mailbox)

    def unsupported_throttle_streak(self, mailbox: MailboxRef) -> int:
        """How many consecutive THROTTLE evaluations in a row have come back
        UNSUPPORTED for this mailbox (CLOSE3-2). `0` if it's never happened,
        or has since been reset by any other outcome."""
        return self._unsupported_throttle_streak.get(mailbox, 0)

    def mark_unsupported_throttle(self, mailbox: MailboxRef) -> None:
        self._unsupported_throttle_streak[mailbox] = self.unsupported_throttle_streak(mailbox) + 1

    def clear_unsupported_throttle_streak(self, mailbox: MailboxRef) -> None:
        self._unsupported_throttle_streak.pop(mailbox, None)

    def mark_throttled(self, mailbox: MailboxRef, *, current_daily_limit: int) -> None:
        self._status[mailbox] = MailboxBreakerStatus.THROTTLED
        self._throttled_at_limit[mailbox] = current_daily_limit

    def mark_pause_in_flight(self, mailbox: MailboxRef) -> None:
        self._status[mailbox] = MailboxBreakerStatus.PAUSE_IN_FLIGHT

    def mark_paused(self, mailbox: MailboxRef) -> None:
        self._status[mailbox] = MailboxBreakerStatus.PAUSED

    def mark_pause_failed(self, mailbox: MailboxRef) -> None:
        """A pause attempt got a definitive FAILED from the provider
        (CLOSE-3c). Deliberately distinct from `mark_active`: this mailbox
        has NOT been verified healthy, so it must not look pristine to a
        subsequent THROTTLE evaluation. Unlike `mark_active`, this does
        *not* clear `throttled_at_limit` -- that memory is exactly what
        keeps a later THROTTLE idempotent instead of re-halving."""
        self._status[mailbox] = MailboxBreakerStatus.PAUSE_FAILED

    def mark_active(self, mailbox: MailboxRef) -> None:
        """For a pause attempt that's definitively UNSUPPORTED (the provider
        structurally can't pause this target), for a genuine recovery (a
        sustained OK verdict clearing a THROTTLED mailbox -- CLOSE-3b), and
        for `resume_after_human_review` below. Not for "response lost"
        (which must stay PAUSE_IN_FLIGHT so the next tick reconciles) or a
        FAILED pause (see `mark_pause_failed`), and never for un-pausing a
        confirmed-paused mailbox on its own.

        Clears `throttled_at_limit`: ACTIVE means this mailbox is being
        treated as pristine again, so a future THROTTLE must not be
        compared against a limit from a throttle episode that's now over.
        """
        self._status[mailbox] = MailboxBreakerStatus.ACTIVE
        self._throttled_at_limit.pop(mailbox, None)
        self._unsupported_throttle_streak.pop(mailbox, None)

    def resume_after_human_review(self, mailbox: MailboxRef) -> None:
        """The only way back from PAUSED to ACTIVE. Not called anywhere in
        this module's automatic evaluation path. See ADR 0003."""
        self._status[mailbox] = MailboxBreakerStatus.ACTIVE
        self._throttled_at_limit.pop(mailbox, None)
        self._unsupported_throttle_streak.pop(mailbox, None)


# A mailbox throttled all the way to 0/day is a pause wearing a different
# hat -- it blurs the ladder's rungs into each other. 1 is the floor.
_MIN_THROTTLED_DAILY_LIMIT = 1

# CLOSE3-2: how many consecutive THROTTLE evaluations may come back
# UNSUPPORTED before escalating to PAUSE. A provider that can't execute the
# throttle -- unknown `current_daily_limit`, or a driver that structurally
# has no throttle primitive at all -- must not write an identical
# UNSUPPORTED record forever; a bounded streak still gives a few evaluations'
# worth of benefit of the doubt (e.g. a transient gap in reported data)
# before routing through the human-review gate (ADR 0003), same as the
# floor-escalation case just below.
_MAX_UNSUPPORTED_THROTTLE_STREAK = 3


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
    applied_daily_limit: int | None = None
    """Set exactly when `verdict is THROTTLE` and `action.outcome is
    PERFORMED` (fresh throttle, idempotent repeat, or re-throttle after
    growth alike): the `current_daily_limit` INPUT the mailbox is currently
    locked to, i.e. `state_store.throttled_at_limit(mailbox)` immediately
    after `_act` runs. `None` otherwise. This is what
    `audit.log.DecisionRecord.applied_daily_limit` persists, and what
    `BreakerStateStore.from_log` restores `_throttled_at_limit` from
    (CLOSE3-1) -- without it, a process that restarts between every
    evaluation (e.g. `check` run from cron) has no memory of what it already
    applied and re-halves on every single invocation."""


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

    When `peer_group` is given, the VERDICT (and the `lower_bound` returned
    on `BreakerEvaluation`) is the WORSE of the pooled and flat lower bounds
    -- never just the pooled one (ADR 0007). The ESS cap alone still let a
    large healthy peer group make the breaker read a mailbox with enough of
    its OWN bad evidence to breach on its own as healthy once pooled, at
    own-volume levels the cap didn't bound. `BreakerEvaluation.posterior`
    itself is unaffected by this and remains the raw pooled posterior, for
    audit/inspection -- only which lower bound decides the verdict changes.

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

    `sends == 0` OR `complaints > sends` both mean the same thing --
    there isn't enough coherent data to compute a posterior at all -- and
    both produce `DataState.INSUFFICIENT_DATA`/`Verdict.OK`/`action=None`
    (CLOSE7-2). `complaints > sends` is not a caller bug: bounce/complaint
    feedback lags sends by 24h-3 days (README's own documented lag), so a
    real provider's aggregation window can legitimately report a bounce
    against a day whose matching sends haven't landed, or already rolled
    out of the window. `sends < 0` and `complaints < 0` remain `ValueError`
    -- those genuinely cannot happen from any real count, coherent or not.
    """
    if sends < 0:
        raise ValueError(f"sends must be >= 0, got {sends}")
    if complaints < 0:
        raise ValueError(f"complaints must be >= 0, got {complaints}")

    # CLOSE7-2: `complaints > sends` is data a real provider can legitimately
    # produce -- bounce/complaint feedback lags sends by 24h-3 days (this
    # project's own README), so a query window can catch a bounce reported
    # against a day whose send count hasn't landed yet, or has already
    # rolled out of the window. That is a DATA-QUALITY condition, not a
    # programmer error: raising `ValueError` for it (as this function used
    # to) tracebacks a real evaluation loop out of `cli.cmd_check`, entirely
    # outside the documented exit-code map. Treated the same way `sends ==
    # 0` already is -- `DataState.INSUFFICIENT_DATA`, `Verdict.OK`, no
    # action -- rather than special-cased differently: both are "the
    # numbers on hand do not support a verdict," which is exactly what
    # `DataState` exists to represent (`engine/state.py`'s own module
    # docstring).
    data_is_sufficient = sends > 0 and complaints <= sends

    if compliance_gate_tripped:
        posterior = (
            _posterior_for(prior, sends, complaints, peer_group, max_pooled_ess)
            if data_is_sufficient
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
            data_state=DataState.OK if data_is_sufficient else DataState.INSUFFICIENT_DATA,
            prior=prior,
            posterior=posterior,
            lower_bound=lower_bound,
            confidence=confidence,
            thresholds=thresholds,
            verdict=Verdict.PAUSE,
            dry_run=dry_run,
            action=action,
        )

    if not data_is_sufficient:
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
    if peer_group is not None:
        # ADR 0002's CLOSE-2 addendum: pooling must never make the breaker
        # LESS sensitive than evaluating this mailbox's own evidence alone
        # would. Between roughly 91 and 389 own sends, the pooled posterior
        # was strictly quieter than the flat one -- a mailbox breaching at
        # 5% (16x Gmail's ceiling) on its own evidence read as healthy once
        # pooled with enough healthy peers. Taking the worse (higher) of the
        # two lower bounds guarantees pooling only ever ADDS sensitivity:
        # the flat evaluation is always still checked, so a mailbox with
        # enough of its own evidence to breach on its own keeps breaching
        # regardless of its peer group, while a mailbox with too little of
        # its own evidence to say anything (the legitimate case pooling
        # exists for) is unaffected, since its flat lower bound is small
        # enough that the pooled one -- healthy or not -- still wins the max.
        lower_bound = max(lower_bound, update(prior, sends, complaints).lower_bound(confidence))
    verdict = rung(lower_bound, thresholds)

    if (
        verdict is Verdict.THROTTLE
        and current_daily_limit is not None
        and current_daily_limit // 2 <= _MIN_THROTTLED_DAILY_LIMIT
    ):
        # Escalate: a throttle whose RESULT would be at or below the floor
        # is a pause wearing a different hat (see
        # `_MIN_THROTTLED_DAILY_LIMIT`'s docstring) -- not just one that
        # would fall strictly below it. A limit of 2 or 3 halves to exactly
        # 1 (the floor) without this `<=`, silently clamping a mailbox to a
        # de-facto pause with no human gate (CLOSE-3d). Route it through
        # PAUSE -- and therefore through the human-review gate (ADR 0003)
        # -- instead.
        verdict = Verdict.PAUSE
    elif (
        verdict is Verdict.THROTTLE
        and state_store.unsupported_throttle_streak(mailbox) >= _MAX_UNSUPPORTED_THROTTLE_STREAK
    ):
        # CLOSE3-2: a throttle this provider has been unable to execute
        # `_MAX_UNSUPPORTED_THROTTLE_STREAK` times in a row -- unknown
        # `current_daily_limit`, or a driver with no throttle primitive at
        # all -- is not a rung this mailbox can ever actually descend.
        # Escalate through PAUSE (and therefore the human-review gate, ADR
        # 0003) instead of writing another identical UNSUPPORTED record.
        verdict = Verdict.PAUSE
    elif verdict is Verdict.OK and state_store.status_of(mailbox) is MailboxBreakerStatus.THROTTLED:
        # CLOSE-3b: a sustained OK verdict is the ladder's own recovery path
        # for THROTTLE (unlike PAUSE, which never auto-recovers -- ADR
        # 0003). Without this, a mailbox that gets throttled once stays
        # THROTTLED forever even after it's genuinely healthy again, and a
        # later re-degradation reads as an idempotent no-op instead of a
        # fresh throttle.
        state_store.mark_active(mailbox)

    if verdict is not Verdict.THROTTLE:
        # Reset CLOSE3-2's streak on any outcome other than a THROTTLE
        # verdict -- OK, WARN, or an escalation to PAUSE just above all mean
        # this mailbox is no longer in the "repeatedly unexecutable
        # throttle" situation the streak tracks.
        state_store.clear_unsupported_throttle_streak(mailbox)

    action: ActionResult | None = None
    applied_daily_limit: int | None = None
    if verdict in (Verdict.THROTTLE, Verdict.PAUSE):
        action = _act(
            driver,
            mailbox,
            verdict,
            state_store,
            dry_run=dry_run,
            current_daily_limit=current_daily_limit,
        )
        if verdict is Verdict.THROTTLE:
            if action.outcome is ActionOutcome.PERFORMED:
                applied_daily_limit = state_store.throttled_at_limit(mailbox)
                state_store.clear_unsupported_throttle_streak(mailbox)
            elif action.outcome is ActionOutcome.UNSUPPORTED:
                state_store.mark_unsupported_throttle(mailbox)
            else:  # FAILED
                state_store.clear_unsupported_throttle_streak(mailbox)

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
        applied_daily_limit=applied_daily_limit,
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
        if state_store.status_of(mailbox) is MailboxBreakerStatus.PAUSED:
            # CLOSE5-1: a mailbox already PAUSED is behind ADR 0003's
            # human-review gate, and nothing automatic may touch its
            # sending limits in either direction -- mirrors the PAUSE
            # branch's own already-PAUSED short-circuit below, forty lines
            # down, which this branch never had. Without this, `evaluate()`
            # computes a fresh verdict from today's evidence with no idea
            # the mailbox is paused (neither `evaluate()` nor this function
            # consulted `status_of` before reaching here), so a PAUSED
            # mailbox whose evidence happened to land in the THROTTLE band
            # got a REAL `driver.throttle()` call and `mark_throttled` moved
            # it to THROTTLED -- from which a single later OK evaluation
            # reaches ACTIVE via CLOSE-3b's sustained-recovery path, with no
            # human-review action of any kind ever recorded. Returned as
            # PERFORMED/idempotent, not UNSUPPORTED: this isn't the provider
            # refusing anything, it's this breaker refusing to ask -- the
            # same framing the PAUSE
            # branch already uses for "a previous trip already got this
            # mailbox paused."
            return ActionResult(
                outcome=ActionOutcome.PERFORMED,
                detail="mailbox is paused; throttle refused pending human review (ADR 0003)",
                capability=Capability.THROTTLE,
            )
        # Idempotent, keyed on the (verdict, applied limit) pair, not just
        # the status (CLOSE-3b/ENG-5a): a mailbox is a no-op repeat only if
        # its current daily limit hasn't grown past what we last throttled
        # it against. This survives an intervening FAILED pause attempt on
        # purpose (`mark_pause_failed` doesn't clear this memory) -- that's
        # CLOSE-3c: a failed pause must not make the next THROTTLE re-halve
        # an already-throttled limit as if the mailbox were pristine.
        recorded_limit = state_store.throttled_at_limit(mailbox)
        if recorded_limit is not None and (
            current_daily_limit is None or current_daily_limit <= recorded_limit
        ):
            return ActionResult(
                outcome=ActionOutcome.PERFORMED,
                detail="mailbox already throttled at this limit; no action taken (idempotent)",
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
            state_store.mark_throttled(mailbox, current_daily_limit=current_daily_limit)
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
        case ActionOutcome.UNSUPPORTED:
            # A clean, definitive answer: this provider will never support
            # pausing this target, so there's nothing ambiguous left to
            # reconcile, and no reason to treat this mailbox as anything but
            # pristine going forward.
            state_store.mark_active(mailbox)
        case ActionOutcome.FAILED:
            # CLOSE-3c: also a definitive answer -- the provider explicitly
            # said no -- but NOT "verified healthy." Marking this ACTIVE let
            # a subsequent THROTTLE verdict see a mailbox that still needed
            # a real limit reduction as pristine, re-halving an
            # already-throttled limit (25 -> 12) instead of staying
            # idempotent against what was already applied.
            state_store.mark_pause_failed(mailbox)
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
