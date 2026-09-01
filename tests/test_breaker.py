"""Tests for engine/breaker.py: the ladder, idempotent pause, and dry-run identity."""

import dataclasses
import itertools
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from deliverability_guard.audit.log import (
    DecisionRecord,
    ResumeRecord,
    append_record,
    append_resume_record,
    read_records,
)
from deliverability_guard.engine.breaker import (
    DEFAULT_LADDER,
    BreakerEvaluation,
    BreakerStateStore,
    BreakerStateStoreLoadError,
    MailboxBreakerStatus,
    ThresholdLadder,
    ThresholdStore,
    Verdict,
    evaluate,
    rung,
)
from deliverability_guard.engine.posterior import DEFAULT_PRIOR, GroupObservation
from deliverability_guard.engine.state import DataState
from deliverability_guard.providers.base import (
    ActionOutcome,
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
    RateLimitExceededError,
)
from fixtures.fake_driver import FakeDriver

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_MAILBOX = MailboxRef(provider="fake", mailbox_id="a@example.com")


# --- rung() and ThresholdLadder -----------------------------------------


def test_rung_below_warn_is_ok() -> None:
    assert rung(0.0001, DEFAULT_LADDER) == Verdict.OK


def test_rung_at_warn_is_warn() -> None:
    assert rung(DEFAULT_LADDER.warn, DEFAULT_LADDER) == Verdict.WARN


def test_rung_at_throttle_is_throttle() -> None:
    assert rung(DEFAULT_LADDER.throttle, DEFAULT_LADDER) == Verdict.THROTTLE


def test_rung_at_pause_is_pause() -> None:
    assert rung(DEFAULT_LADDER.pause, DEFAULT_LADDER) == Verdict.PAUSE


def test_rung_above_pause_is_still_pause() -> None:
    assert rung(1.0, DEFAULT_LADDER) == Verdict.PAUSE


def test_threshold_ladder_rejects_out_of_order_values() -> None:
    with pytest.raises(ValueError, match="warn"):
        ThresholdLadder(warn=0.002, throttle=0.001, pause=0.003)


def test_threshold_store_swap_is_a_full_replacement() -> None:
    store = ThresholdStore(DEFAULT_LADDER)
    tighter = ThresholdLadder(warn=0.0001, throttle=0.0002, pause=0.0003)
    store.swap(tighter)
    assert store.current == tighter


# --- evaluate(): data state -----------------------------------------------


def test_zero_sends_is_insufficient_data_and_takes_no_action() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=0,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert result.verdict == Verdict.OK
    assert result.action is None
    assert driver.pause_calls == []
    assert driver.throttle_calls == []


def test_rejects_negative_sends() -> None:
    with pytest.raises(ValueError, match="sends"):
        evaluate(
            driver=FakeDriver(),
            mailbox=_MAILBOX,
            sends=-1,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=_NOW,
        )


def test_rejects_negative_complaints() -> None:
    with pytest.raises(ValueError, match="complaints"):
        evaluate(
            driver=FakeDriver(),
            mailbox=_MAILBOX,
            sends=5,
            complaints=-1,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=_NOW,
        )


def test_complaints_greater_than_sends_is_insufficient_data_not_a_valueerror() -> None:
    """CLOSE7-2: `complaints > sends` used to raise `ValueError` -- a
    programmer-error framing for something a real provider can
    legitimately produce (bounce/complaint feedback lags sends by 24h-3
    days; a query window can catch a bounce for a day whose sends haven't
    landed, or already rolled out). Treated the same as `sends == 0` --
    `DataState.INSUFFICIENT_DATA`, `Verdict.OK`, no action -- rather than
    a traceback that would propagate out of a real evaluation loop."""
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5,
        complaints=6,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert result.data_state is DataState.INSUFFICIENT_DATA
    assert result.verdict is Verdict.OK
    assert result.action is None
    assert result.posterior is None
    assert result.lower_bound is None
    assert driver.pause_calls == []
    assert driver.throttle_calls == []


def test_complaints_greater_than_sends_under_compliance_gate_still_pauses(tmp_path: Path) -> None:
    """The compliance-forced-PAUSE analogue: incoherent counts under a
    hard compliance gate must not crash either, and the gate still wins --
    Google's own account-level verdict is independent of today's send
    volume or its internal consistency."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5,
        complaints=6,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=True,
    )
    assert result.verdict is Verdict.PAUSE
    assert result.data_state is DataState.INSUFFICIENT_DATA
    assert result.posterior is None
    assert driver.pause_calls == [_MAILBOX]


# --- evaluate(): the ladder itself ----------------------------------------


def test_healthy_mailbox_is_ok_and_takes_no_action() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert result.verdict == Verdict.OK
    assert result.action is None


def test_warn_verdict_takes_no_provider_action() -> None:
    """Warn is notify-only -- the ladder never calls the provider for it."""
    driver = FakeDriver()
    # Handcrafted to land in the warn band: enough complaints that the
    # lower bound sits between warn and throttle.
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=20,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert result.verdict == Verdict.WARN
    assert result.action is None
    assert driver.pause_calls == []
    assert driver.throttle_calls == []


def test_throttle_verdict_reduces_daily_limit_by_half() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert result.verdict == Verdict.THROTTLE
    assert driver.throttle_calls == [("a@example.com", 50)]


def test_throttle_verdict_without_known_daily_limit_is_unsupported() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=None,
    )
    assert result.action is not None
    assert result.action.outcome == ActionOutcome.UNSUPPORTED
    assert driver.throttle_calls == []


def test_throttle_that_would_drop_below_the_floor_escalates_to_pause() -> None:
    """ENG-5a: a mailbox already down to a daily limit of 1 has nowhere left
    to throttle -- halving it again would floor-clamp to 1 forever, which is
    a pause wearing a different hat (see the module docstring). The correct
    response is to escalate to PAUSE, which goes through the human-review
    gate (ADR 0003), not to silently keep "throttling" at the same floor.

    This deliberately changes the old assertion
    (`driver.throttle_calls == [("a@example.com", 1)]`), which encoded the
    exact bug this fixes: a throttle floor-clamped forever, never reaching
    PAUSE and never passing through human review."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=1,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.throttle_calls == []
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- CLOSE5-1: a PAUSED mailbox must never be throttled --------------------


def test_a_paused_mailbox_that_evaluates_to_throttle_is_not_throttled() -> None:
    """CLOSE5-1: `_act`'s THROTTLE branch never consulted
    `state_store.status_of`, unlike its PAUSE branch -- so a mailbox already
    behind the human-review gate (ADR 0003) got a REAL `driver.throttle()`
    call the moment its evidence happened to compute THROTTLE instead of
    PAUSE, and `mark_throttled` moved it to THROTTLED. Nothing about that
    involves a human."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    state_store.mark_paused(_MAILBOX)

    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,  # 0.15% -- THROTTLE band on its own evidence
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )

    assert result.verdict == Verdict.THROTTLE
    assert driver.throttle_calls == []
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    assert state_store.throttled_at_limit(_MAILBOX) is None


def test_a_paused_mailbox_with_an_ok_verdict_stays_paused() -> None:
    """CLOSE-3b's sustained-recovery path only ever fires for THROTTLED, not
    PAUSED -- confirming that stays true after CLOSE5-1's fix, since the
    reproduction reaches ACTIVE via THROTTLED, never directly from PAUSED."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    state_store.mark_paused(_MAILBOX)

    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )

    assert result.verdict == Verdict.OK
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_act_checks_paused_status_before_ever_calling_throttle() -> None:
    """CLOSE5-1's guard, in the idiom
    `test_evaluate_never_calls_resume_after_human_review` already uses: a
    crude source-level check, but it is what would have caught this. `_act`
    must consult `MailboxBreakerStatus.PAUSED` before it can ever reach
    `effective_driver.throttle(...)` -- the same ordering its PAUSE branch
    has always had relative to `effective_driver.pause(...)`.

    CLOSE8-3: this guard used raw `inspect.getsource`, unguarded -- `_act`
    has no docstring (so CLOSE7-3's `source_body` fix alone would not have
    caught this), but raw `getsource` ALSO includes comments, and a
    mutation that deleted the real check while leaving behind a comment
    naming `MailboxBreakerStatus.PAUSED` passed this test unchanged. Now
    routed through `source_body` (which strips comments too, as of
    CLOSE8-3), so `.index()` can only find the name in actual CODE --
    and raises `ValueError`, failing this test outright, if the mutation
    removes the check entirely and leaves nothing but a comment behind."""
    from deliverability_guard.engine import breaker as breaker_module
    from fixtures.source_inspect import source_body

    source = source_body(breaker_module._act)  # pyright: ignore[reportPrivateUsage]
    paused_check_index = source.index("MailboxBreakerStatus.PAUSED")
    throttle_call_index = source.index("effective_driver.throttle(")
    assert paused_check_index < throttle_call_index, (
        "_act must consult PAUSED status before ever calling driver.throttle() "
        "-- CLOSE5-1: a PAUSED mailbox must never receive a real throttle call"
    )


def test_act_paused_status_guard_catches_the_comment_only_mutation_the_vacuous_one_missed() -> None:
    """CLOSE8-3's own reproduction, kept as a regression test, in the same
    shape as `test_check_and_run_drift_guard_catches_a_hand_duplicated_
    loop_the_vacuous_one_missed` in `tests/test_cli.py`: a stand-in
    function shaped exactly like the audit's own mutation -- the real
    `MailboxBreakerStatus.PAUSED` check replaced with `if False:`, with a
    COMMENT left behind naming it, and no docstring at all (so this is a
    genuinely different failure mode than CLOSE7-3's docstring-only one).

    The vacuous (pre-CLOSE8-3) guard -- raw `inspect.getsource` -- passes
    against this, because the comment mention is enough for `.index()` to
    find both names in the right order. The fixed guard (`source_body`
    with comment-stripping) must not: with the comment gone, `.index()`
    can't find `MailboxBreakerStatus.PAUSED` in the code at all and raises
    `ValueError`, which is a genuine test failure, not a silent pass."""
    import inspect

    from fixtures.source_inspect import source_body

    def mutated_act(verdict: str) -> str:
        # MUTATION (audit): the real MailboxBreakerStatus.PAUSED guard is
        # gone; only this comment mentions MailboxBreakerStatus.PAUSED now.
        if verdict == "THROTTLE":
            if False:
                return "refused"
            return "effective_driver.throttle(mailbox_id, new_limit)"
        return "paused"

    # The vacuous guard this test file used to have: passes purely because
    # the comment mentions the name and happens to sit before the call.
    vacuous_source = inspect.getsource(mutated_act)
    assert vacuous_source.index("MailboxBreakerStatus.PAUSED") < vacuous_source.index(
        "effective_driver.throttle("
    )
    # The fixed guard: the name only ever existed in a comment, so once
    # comments are stripped it isn't in the code at all.
    with pytest.raises(ValueError, match="substring not found"):
        source_body(mutated_act).index("MailboxBreakerStatus.PAUSED")


def test_engine_breaker_module_never_constructs_a_campaign_ref() -> None:
    """CLOSE5-3: the README's/CHANGELOG's CLOSE4-3 claim -- 'the ladder only
    ever constructs a `MailboxRef`, never a `CampaignRef`' -- was true when
    written but had no executable guard, so nothing would fail if it
    stopped being true. Same source-level idiom as
    `test_evaluate_never_calls_resume_after_human_review` and
    `test_act_checks_paused_status_before_ever_calling_throttle` just
    above: this repo already has the pattern for exactly this kind of
    claim, and it should be used every time one is made, not only for the
    ones that happened to get caught by an audit."""
    import inspect

    from deliverability_guard.engine import breaker as breaker_module

    assert "CampaignRef" not in inspect.getsource(breaker_module)


# --- CLOSE3-2: an unexecutable throttle must not loop forever -------------


def test_unsupported_throttle_escalates_to_pause_after_a_bounded_streak() -> None:
    """CLOSE3-2: before this, a mailbox whose current daily limit is unknown
    (or whose provider structurally can't throttle) stayed ACTIVE forever,
    re-deriving THROTTLE and re-emitting an identical UNSUPPORTED record on
    every single evaluation. After a bounded number of consecutive
    unexecutable throttles, this must escalate to PAUSE -- through the
    human-review gate (ADR 0003), same as the floor-escalation case just
    above."""
    driver = FakeDriver(throttle_outcome=ActionOutcome.UNSUPPORTED)
    state_store = BreakerStateStore()
    results = [
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
        for _ in range(4)
    ]
    assert [r.verdict for r in results] == [
        Verdict.THROTTLE,
        Verdict.THROTTLE,
        Verdict.THROTTLE,
        Verdict.PAUSE,
    ]
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_a_successful_throttle_resets_the_unsupported_streak() -> None:
    """The streak is specific to consecutive unexecutable throttles -- a
    successful throttle in between must reset it, not let it silently
    accumulate toward an escalation the mailbox no longer deserves."""
    driver = FakeDriver(throttle_outcome=ActionOutcome.UNSUPPORTED)
    state_store = BreakerStateStore()
    for _ in range(2):  # below the escalation bound of 3
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
    driver.throttle_outcome = ActionOutcome.PERFORMED
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert result.verdict == Verdict.THROTTLE
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED


def test_state_store_from_log_bounds_repeated_unsupported_throttle_records(
    tmp_path: Path,
) -> None:
    """CLOSE3-2's own required test shape: ten separate restarts (a fresh
    `BreakerStateStore.from_log` each time), a driver that always reports
    UNSUPPORTED for throttle. The log must not accumulate ten identical
    UNSUPPORTED throttle records -- the streak persists across restarts via
    `from_log`, same as `_throttled_at_limit` (CLOSE3-1)."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(throttle_outcome=ActionOutcome.UNSUPPORTED)
    for _ in range(10):
        # CLOSE4-2: all ten iterations run, unconditionally -- unattended
        # re-running is exactly the documented deployment (`check` is "the
        # smallest thing you can put in cron," and nobody watches cron).
        # Breaking out early on PAUSED status used to be the only thing
        # standing between this test and CLOSE4-1's bug: once escalated to
        # PAUSE, a THROTTLE/UNSUPPORTED record replayed afterward used to
        # un-pause the mailbox, and the very next tick would throttle-escalate
        # and pause it AGAIN -- a real provider call every four runs, forever,
        # not the one call this test now proves happens.
        state_store = (
            BreakerStateStore.from_log(log_path) if log_path.exists() else BreakerStateStore()
        )
        result = evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
        append_record(log_path, DecisionRecord.from_evaluation(result))

    records = read_records(log_path)
    unsupported_throttle_records = [
        r
        for r in records
        if r.verdict is Verdict.THROTTLE and r.action_outcome is ActionOutcome.UNSUPPORTED
    ]
    assert len(unsupported_throttle_records) < 10
    assert BreakerStateStore.from_log(log_path).status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    # CLOSE4-1/CLOSE4-2: once escalated to PAUSE, a THROTTLE/UNSUPPORTED
    # record replayed afterward must never un-pause the mailbox -- so
    # exactly ONE real provider pause call happens across all ten runs,
    # not one every time the streak re-crosses the bound.
    assert driver.pause_calls == [_MAILBOX]


# --- Idempotency: THROTTLE, keyed on the verdict, not the limit ------------


def test_repeated_throttle_verdict_does_not_re_halve_the_daily_limit() -> None:
    """ENG-5a's reproduction: six identical THROTTLE evaluations against one
    mailbox must not compound (50 -> 25 -> 12 -> 6 -> 3 -> 1). Only the
    first evaluation actually calls throttle(); the rest are idempotent
    no-ops, exactly like repeated PAUSE."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    for _ in range(6):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=30,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=100,
        )
    assert driver.throttle_calls == [("a@example.com", 50)]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED


def test_failed_throttle_does_not_mark_the_mailbox_throttled() -> None:
    """If the provider call itself fails, the mailbox must not be marked
    THROTTLED -- that would make a genuinely failed throttle silently
    idempotent-no-op on every future retry."""
    driver = FakeDriver(throttle_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert driver.throttle_calls == [("a@example.com", 50)]


def test_throttle_idempotency_does_not_block_a_first_time_pause() -> None:
    """A mailbox already THROTTLED must still PAUSE normally once its
    evidence escalates to the pause rung -- idempotency is per-verdict, not
    a blanket "never act on this mailbox again"."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED

    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- CLOSE-3: throttle recovery, re-throttle, floor escalation off-by-one --


def test_throttle_then_ok_then_throttle_re_throttles() -> None:
    """CLOSE-3b: a sustained OK verdict clears THROTTLED back to ACTIVE --
    the ladder's own recovery path for THROTTLE, unlike PAUSE. A later
    re-degradation must reach the provider again, not be swallowed as an
    idempotent no-op against a status that never recovered."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED

    ok_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    assert ok_result.verdict == Verdict.OK
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE

    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    assert driver.throttle_calls == [("a@example.com", 50), ("a@example.com", 25)]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED


def test_throttle_re_throttles_when_the_current_limit_has_grown_past_what_was_applied() -> None:
    """CLOSE-3b/ENG-5a: the idempotency key is (verdict, applied limit), not
    just the status. A mailbox stays THROTTLED throughout here (no
    intervening OK), but its current limit is later reported HIGHER than
    what the breaker last applied -- evidence something restored it outside
    the breaker's own bookkeeping. That must reach the provider again, not
    be swallowed as idempotent."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,  # -> throttled to 50
    )
    assert driver.throttle_calls == [("a@example.com", 50)]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED

    # An identical re-evaluation with the SAME current_daily_limit (100) is
    # still idempotent, exactly like the six-identical-ticks case.
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert driver.throttle_calls == [("a@example.com", 50)]

    # Now the mailbox's real current limit is reported HIGHER than 100 --
    # something restored it beyond where the breaker last found it.
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=150,
    )
    assert driver.throttle_calls == [("a@example.com", 50), ("a@example.com", 75)]


def test_failed_pause_does_not_let_a_later_throttle_re_halve() -> None:
    """CLOSE-3c's reproduction: THROTTLE -> 25; a PAUSE attempt in between
    FAILS; the next THROTTLE (against the same, unchanged current limit)
    must stay idempotent, not halve again (25 -> 12)."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,  # -> throttled to 25
    )
    assert driver.throttle_calls == [("a@example.com", 25)]

    driver.pause_outcome = ActionOutcome.FAILED
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=25,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_FAILED

    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=25,
    )
    assert driver.throttle_calls == [("a@example.com", 25)]  # NOT halved again to 12


def test_daily_limit_of_two_escalates_to_pause() -> None:
    """CLOSE-3d: `2 // 2 == 1`, exactly the floor -- must escalate, not
    silently clamp to a de-facto pause with no human gate."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=2,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.throttle_calls == []
    assert driver.pause_calls == [_MAILBOX]


def test_daily_limit_of_three_escalates_to_pause() -> None:
    """CLOSE-3d: `3 // 2 == 1`, also exactly the floor."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=3,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.throttle_calls == []
    assert driver.pause_calls == [_MAILBOX]


def test_daily_limit_of_four_still_throttles_normally() -> None:
    """`4 // 2 == 2`, meaningfully above the floor -- must NOT escalate."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=4,
    )
    assert result.verdict == Verdict.THROTTLE
    assert driver.throttle_calls == [("a@example.com", 2)]
    assert driver.pause_calls == []


def test_pause_verdict_pauses_and_marks_state() -> None:
    driver = FakeDriver()
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- Idempotency: no double-pause ------------------------------------------


def test_repeated_pause_verdict_does_not_call_pause_twice() -> None:
    """Breaker trips while a previous trip is still in flight (here:
    already confirmed paused) -> idempotent, no double-pause."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    for _ in range(3):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=40,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
        )
    assert driver.pause_calls == [_MAILBOX]  # only the first evaluation actually called pause()


def test_pause_failure_marks_pause_failed_not_active() -> None:
    """CLOSE-3c: a FAILED pause is a definitive answer, but NOT "verified
    healthy" -- it must not look like ACTIVE (pristine) to a later THROTTLE
    evaluation. This deliberately changes the old assertion (`== ACTIVE`),
    which encoded the exact bug this fixes: a failed pause reset a
    mailbox's status such that a subsequent THROTTLE re-halved an
    already-throttled limit instead of staying idempotent."""
    driver = FakeDriver(pause_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_FAILED


def test_pause_failed_recovers_after_thirty_healthy_days() -> None:
    """CLOSE9-1's reproduction, run to completion: `mark_pause_failed`
    deliberately keeps `throttled_at_limit` alive for the NEXT evaluation
    (CLOSE-3c) -- correct advice for that one evaluation, but before this
    fix nothing ever moved the mailbox OFF `PAUSE_FAILED` at all, so that
    memory latched forever. Thirty healthy evaluations, then a fresh
    breach against a real, changed operator limit -- must produce a REAL
    provider call, not the "already throttled at this limit" idempotent
    no-op a permanently stale memory would keep producing."""
    driver = FakeDriver()
    state_store = BreakerStateStore()

    # 1. A real throttle at limit 400 -> halved to 200.
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=400,
    )
    assert driver.throttle_calls == [("a@example.com", 200)]
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED

    # 2. A pause attempt FAILS.
    driver.pause_outcome = ActionOutcome.FAILED
    evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=200,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_FAILED

    # 3. Thirty consecutive healthy evaluations.
    for _ in range(30):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=200,
        )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert state_store.throttled_at_limit(_MAILBOX) is None

    # 4. A fresh breach. The operator has since restored the real daily
    # limit to 100 (down from the stale 400 on file) -- a genuinely new
    # THROTTLE, not a repeat of the old one.
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert result.verdict is Verdict.THROTTLE
    assert result.action is not None
    assert result.action.outcome is ActionOutcome.PERFORMED
    assert driver.throttle_calls == [("a@example.com", 200), ("a@example.com", 50)]


def test_pause_unsupported_reverts_status_to_active() -> None:
    driver = FakeDriver(capabilities=frozenset({Capability.READ_STATS}))
    state_store = BreakerStateStore()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert result.action is not None
    assert result.action.outcome == ActionOutcome.UNSUPPORTED
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_lost_response_stays_pause_in_flight_and_propagates_the_error() -> None:
    """Provider pause succeeds but the response is lost -> reconcile on next
    tick. Here: the call raises entirely (the strongest form of "lost
    response"). Status must stay PAUSE_IN_FLIGHT, not silently revert to
    ACTIVE or jump to PAUSED -- we genuinely don't know which happened."""
    driver = FakeDriver(raise_on_pause=RateLimitExceededError("rate limited"))
    state_store = BreakerStateStore()
    with pytest.raises(RateLimitExceededError):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=40,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
        )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_IN_FLIGHT


def test_reconciliation_retries_pause_on_the_next_tick() -> None:
    """After a lost response, the next evaluation tick re-attempts the
    pause -- real pause endpoints are idempotent, so this is reconciliation,
    not a double-pause."""
    driver = FakeDriver(raise_on_pause=RateLimitExceededError("rate limited"))
    state_store = BreakerStateStore()
    with pytest.raises(RateLimitExceededError):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=40,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
        )
    # Now the provider is healthy again.
    driver.raise_on_pause = None
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    assert len(driver.pause_calls) == 2  # the lost attempt, then the reconciling retry
    assert result.verdict == Verdict.PAUSE
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- BreakerStateStore: never auto-resume ----------------------------------


def test_state_store_defaults_to_active() -> None:
    assert BreakerStateStore().status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_only_resume_after_human_review_moves_paused_back_to_active() -> None:
    store = BreakerStateStore()
    store.mark_paused(_MAILBOX)
    assert store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    store.resume_after_human_review(_MAILBOX)
    assert store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_evaluate_never_calls_resume_after_human_review() -> None:
    """No code path in evaluate() (or its private helper `_act`) can move a
    mailbox from PAUSED back to ACTIVE on its own -- ADR 0003."""
    import inspect

    from deliverability_guard.engine import breaker as breaker_module

    assert "resume_after_human_review" not in inspect.getsource(breaker_module.evaluate)
    assert "resume_after_human_review" not in inspect.getsource(
        breaker_module._act  # pyright: ignore[reportPrivateUsage]
    )


def test_dry_run_parameter_has_no_default_on_evaluate_or_act() -> None:
    """CLOSE6-3: README.md's own claim -- 'no code path can pause or
    throttle without `dry_run=False`, set on purpose' -- had no signature
    guard, despite `tests/test_slow_loop.py`'s
    `test_tune_thresholds_signature_has_no_capability_carrying_parameter`
    already using exactly this `inspect.signature` idiom for a different
    claim in this repo. `evaluate`'s and `_act`'s own docstrings already
    say `dry_run` has no default so every call site must decide
    explicitly -- this is that claim, checked."""
    import inspect

    from deliverability_guard.engine import breaker as breaker_module

    evaluate_signature = inspect.signature(breaker_module.evaluate)
    assert evaluate_signature.parameters["dry_run"].default is inspect.Parameter.empty

    act_signature = inspect.signature(
        breaker_module._act  # pyright: ignore[reportPrivateUsage]
    )
    assert act_signature.parameters["dry_run"].default is inspect.Parameter.empty


# --- Dry-run identity -------------------------------------------------------


def test_dry_run_decision_is_identical_to_live_except_the_flag_and_detail() -> None:
    """AGENTS.md: dry-run must produce decisions IDENTICAL to the live
    path. The only allowed difference is that the provider call itself
    isn't made -- verified here as: everything about the DECISION matches,
    and only `dry_run` and the human-readable action detail differ."""
    live_driver = FakeDriver()
    dry_driver = FakeDriver()

    live = evaluate(
        driver=live_driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    dry = evaluate(
        driver=dry_driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )

    assert live.verdict == dry.verdict
    assert live.data_state == dry.data_state
    assert live.posterior == dry.posterior
    assert live.lower_bound == dry.lower_bound
    assert live.thresholds == dry.thresholds
    assert live.sends == dry.sends
    assert live.complaints == dry.complaints
    assert live.evaluated_at == dry.evaluated_at

    assert live.action is not None
    assert dry.action is not None
    assert live.action.outcome == dry.action.outcome
    assert live.action.capability == dry.action.capability

    # The one place they're allowed to differ.
    assert live.dry_run is False
    assert dry.dry_run is True
    assert live.action.detail != dry.action.detail
    assert "[DRY RUN]" in dry.action.detail
    assert "[DRY RUN]" not in live.action.detail

    # And critically: dry-run never touched the real driver.
    assert dry_driver.pause_calls == []
    assert live_driver.pause_calls == [_MAILBOX]


# --- Compliance hard gate ----------------------------------------------


def test_compliance_gate_forces_pause_even_with_a_healthy_posterior() -> None:
    """Google telling you directly you're non-compliant outranks any
    statistical inference -- trip regardless of volume."""
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=True,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]


def test_compliance_gate_forces_pause_even_with_zero_sends() -> None:
    """The hard gate outranks volume entirely -- even INSUFFICIENT_DATA
    doesn't block it, since the compliance verdict isn't derived from
    today's send volume at all."""
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=0,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=True,
    )
    assert result.verdict == Verdict.PAUSE
    assert result.data_state == DataState.INSUFFICIENT_DATA
    assert result.posterior is None
    assert driver.pause_calls == [_MAILBOX]


def test_compliance_gate_false_does_not_change_normal_behavior() -> None:
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=False,
    )
    assert result.verdict == Verdict.OK
    assert driver.pause_calls == []


# --- Hierarchical pooling wired into evaluate() (ENG-4 part 3) ------------


def test_evaluate_with_peer_group_uses_pooled_posterior() -> None:
    """Wiring check: passing `peer_group` pulls a marginal mailbox's
    POSTERIOR toward its healthy peers' posterior -- proving `evaluate()`
    actually calls `engine.posterior.pooled_posterior` rather than
    `update()` alone.

    This does NOT assert `pooled_result.lower_bound < flat_result.lower_bound`
    (the old version of this test did): CLOSE-2 fixed a bug where pooling
    could make the breaker's EFFECTIVE decision quieter than the flat
    evaluation alone, so `evaluate()` now takes the worse of the pooled and
    flat lower bounds when `peer_group` is given -- see
    `test_evaluate_peer_group_lower_bound_is_never_below_the_flat_one`
    below. The `.posterior` FIELD (used for audit/inspection) is still the
    raw pooled `BetaDistribution`, unaffected by that -- which is what this
    test checks instead."""
    driver = FakeDriver()
    healthy_peers = [GroupObservation(sends=500, complaints=0) for _ in range(40)]
    pooled_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=50,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )
    flat_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=50,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert pooled_result.posterior != flat_result.posterior
    from deliverability_guard.engine.posterior import pooled_posterior

    assert pooled_result.posterior == pooled_posterior(
        DEFAULT_PRIOR, healthy_peers, own_sends=50, own_complaints=1
    )


def test_evaluate_peer_group_lower_bound_is_never_below_the_flat_one() -> None:
    """CLOSE-2: pooling must never make the breaker's effective decision
    LESS sensitive than evaluating this mailbox's own evidence alone would
    -- `evaluate()` takes the worse (higher) of the pooled and flat lower
    bounds whenever `peer_group` is given. Here the peer group is healthy
    enough that the POOLED posterior alone would read this mailbox as
    quieter than its own evidence -- the effective lower bound must still
    match (or exceed) the flat one."""
    driver = FakeDriver()
    healthy_peers = [GroupObservation(sends=500, complaints=0) for _ in range(40)]
    pooled_result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=50,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )
    from deliverability_guard.engine.posterior import update

    flat_lower_bound = update(DEFAULT_PRIOR, sends=50, complaints=1).lower_bound()
    assert pooled_result.lower_bound is not None
    assert pooled_result.lower_bound >= flat_lower_bound


def test_evaluate_peer_group_does_not_mask_a_mailbox_with_enough_of_its_own_evidence() -> None:
    """ENG-4's reproduction, at the breaker level: even wired through
    `evaluate()`, a mailbox with enough of its own bad evidence must still
    trip PAUSE, regardless of how many healthy peers it has."""
    driver = FakeDriver()
    state_store = BreakerStateStore()
    healthy_peers = [GroupObservation(sends=5000, complaints=5) for _ in range(99)]
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=250,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        peer_group=healthy_peers,
    )
    assert result.verdict == Verdict.PAUSE
    assert driver.pause_calls == [_MAILBOX]


# --- CLOSE-2: pooling never masks a breach the flat evaluation would catch -


@pytest.mark.parametrize("own_sends", [1, 10, 50, 100, 200, 500, 1000, 5000])
def test_pooled_breach_at_the_verdict_level_is_true_wherever_flat_breach_is_true(
    own_sends: int,
) -> None:
    """The CLOSE-2 reproduction, as a property: a mailbox sending at a true
    5% complaint rate (16x Gmail's ceiling) against 999 HEALTHY peers must
    breach through `evaluate()`'s pooled path at every own-volume level
    tested, wherever it would breach evaluated flat/alone -- before this
    fix, pooling masked the breach at 100 and 200 own sends specifically
    (a healthy-looking pooled posterior diluted a genuinely bad mailbox's
    own evidence)."""
    own_complaints = round(own_sends * 0.05)
    healthy_peers = [GroupObservation(sends=5000, complaints=5) for _ in range(999)]  # 0.1% each

    flat = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=own_sends,
        complaints=own_complaints,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    pooled = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=own_sends,
        complaints=own_complaints,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )

    if flat.verdict is not Verdict.OK:
        assert pooled.verdict is not Verdict.OK, (
            f"pooling masked a breach at own_sends={own_sends}: "
            f"flat={flat.verdict.name} pooled={pooled.verdict.name}"
        )


def test_n1_at_100_percent_against_999_healthy_peers_still_does_not_breach() -> None:
    """The legitimate case pooling exists for must survive CLOSE-2's fix:
    one complaint in one send, against a domain of 999 perfectly healthy
    peers, still must not breach -- n=1 is not enough evidence to say
    anything, no matter how the peer group looks."""
    healthy_peers = [GroupObservation(sends=5000, complaints=0) for _ in range(999)]
    result = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=1,
        complaints=1,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
        peer_group=healthy_peers,
    )
    assert result.verdict == Verdict.OK


def test_evaluate_without_peer_group_is_unchanged_flat_behavior() -> None:
    """`peer_group` defaults to `None`, which must reproduce the exact flat,
    non-pooled posterior every other test in this file relies on."""
    driver = FakeDriver()
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    from deliverability_guard.engine.posterior import update

    assert result.posterior == update(DEFAULT_PRIOR, sends=5000, complaints=40)


def test_compliance_gate_is_idempotent_like_any_other_pause() -> None:
    driver = FakeDriver()
    state_store = BreakerStateStore()
    for _ in range(3):
        evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            compliance_gate_tripped=True,
        )
    assert driver.pause_calls == [_MAILBOX]


# --- BreakerStateStore: rebuilding from the decision log (ENG-5b) ---------


def test_state_store_rebuilds_paused_status_from_the_decision_log(tmp_path: Path) -> None:
    """The reproduction: a fresh `BreakerStateStore()` (as a process restart
    would create) must not un-pause a mailbox the log says was paused."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_rebuilds_throttled_status_from_the_decision_log(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    # CLOSE3-1: the applied limit itself must also come back, not just the
    # status -- this is what keeps a *second* restart's THROTTLE evaluation
    # idempotent instead of re-halving.
    assert restored.throttled_at_limit(_MAILBOX) == 100


def test_state_store_rebuilds_active_from_a_failed_throttle_in_the_log(tmp_path: Path) -> None:
    """CLOSE3-1: a THROTTLE record whose action did NOT perform (FAILED or
    UNSUPPORTED) must rebuild as ACTIVE with no remembered limit -- mirroring
    `evaluate()`'s own live-path behaviour, where a failed throttle never
    calls `mark_throttled` in the first place."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(throttle_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert restored.throttled_at_limit(_MAILBOX) is None


def test_state_store_rebuilds_throttled_with_no_remembered_limit_from_a_pre_close3_1_record(
    tmp_path: Path,
) -> None:
    """Backward compatibility: a THROTTLE/PERFORMED record written before
    CLOSE3-1 has `applied_daily_limit=None` -- `from_log` must still rebuild
    THROTTLED status from it (as it always has), just without a remembered
    limit to restore. The very next evaluation re-derives a fresh, still
    correct halving rather than crashing or silently guessing."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    record = DecisionRecord.from_evaluation(evaluation)
    pre_close3_1_record = dataclasses.replace(record, applied_daily_limit=None)
    append_record(log_path, pre_close3_1_record)

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    assert restored.throttled_at_limit(_MAILBOX) is None


def test_state_store_rebuild_marks_pause_failed_on_a_failed_pause_attempt(tmp_path: Path) -> None:
    """Mirrors the live-path behavior (CLOSE-3c): a FAILED pause rebuilds as
    PAUSE_FAILED, not ACTIVE -- it was never verified healthy."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(pause_outcome=ActionOutcome.FAILED)
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSE_FAILED


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_later_healthy_evaluations(
    tmp_path: Path,
) -> None:
    """A paused mailbox that later reports near-zero, healthy-looking
    evidence must NOT be read back as un-paused on rebuild -- ADR 0003's
    guarantee (never auto-resume) has to survive a restart, not just an
    in-process run."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    pause_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))

    healthy_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=0,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(healthy_eval))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_a_later_unsupported_throttle(
    tmp_path: Path,
) -> None:
    """CLOSE4-1: on the live path, `_act`'s THROTTLE branch never touches
    `state_store` when the outcome is UNSUPPORTED (no `mark_active` call
    anywhere in that branch) -- so a PAUSED mailbox whose current daily
    limit later reads as unknown stays PAUSED. `from_log` used to
    unconditionally set `status[mailbox] = ACTIVE` for this outcome,
    un-pausing a mailbox the ladder is supposed to leave behind the
    human-review gate (ADR 0003) until a human calls `resume`."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    pause_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))
    assert pause_eval.verdict is Verdict.PAUSE
    assert pause_eval.action is not None
    assert pause_eval.action.outcome is ActionOutcome.PERFORMED

    unsupported_throttle_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=None,
    )
    append_record(log_path, DecisionRecord.from_evaluation(unsupported_throttle_eval))
    assert unsupported_throttle_eval.verdict is Verdict.THROTTLE
    assert unsupported_throttle_eval.action is not None
    assert unsupported_throttle_eval.action.outcome is ActionOutcome.UNSUPPORTED

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    # The streak still increments -- CLOSE3-2's escalation must not be lost
    # by fixing CLOSE4-1.
    assert restored.unsupported_throttle_streak(_MAILBOX) == 1


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_a_later_failed_throttle(
    tmp_path: Path,
) -> None:
    """Same bug, the neighbouring branch: `_act`'s THROTTLE branch only
    calls `mark_throttled` on a PERFORMED outcome -- a FAILED throttle
    (the driver itself rejected the call) never touches `state_store`
    either."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    pause_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))

    failed_driver = FakeDriver(throttle_outcome=ActionOutcome.FAILED)
    failed_throttle_eval = evaluate(
        driver=failed_driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    append_record(log_path, DecisionRecord.from_evaluation(failed_throttle_eval))
    assert failed_throttle_eval.verdict is Verdict.THROTTLE
    assert failed_throttle_eval.action is not None
    assert failed_throttle_eval.action.outcome is ActionOutcome.FAILED

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


# --- What the sweep below still cannot see (CLOSE7-4, updated CLOSE8-4, CLOSE9-5)
#
# Written down deliberately -- per the round-7 audit's own closing
# instruction: the highest-value thing a session like this one can do is
# name what the sweep is still blind to, even where nothing gets closed.
# Kept updated rather than deleted once an item DOES close, or gets
# measured clean, the way `campaign-preflight`'s own report marks a
# finding resolved instead of erasing it -- a list that shows what it
# caught (and what it checked) is worth more than one that only shows
# what's left. Six rounds running, this sweep has found exactly what it
# was pointed at and missed everything it wasn't:
#
# 1. MEASURED CLEAN (external audit, CLOSE9-5): interleaved two-mailbox
#    sequences -- 0/4,096 mismatches. `from_log` keying its dicts by
#    `MailboxRef` does appear to be genuinely fine, now checked rather than
#    assumed. Clean at this depth is not clean forever: the sweep is
#    reproducible and should be re-run whenever `from_log`'s own keying
#    logic changes, not treated as permanently settled.
# 2. MEASURED CLEAN (external audit, CLOSE9-5): a `current_daily_limit`
#    that grows and shrinks non-monotonically across repeated THROTTLE
#    moves in the same sequence -- 0/6,859 mismatches at length 3.
#    CLOSE4-1's `is_idempotent_replay` comparison holds outside the
#    "never changes" / "grows once" cases this project had actually
#    tested before. Same caveat as item 1: reproducible, not permanent.
# 3. CLOSED (CLOSE8-1). `compliance_gate_tripped` (the hard PAUSE gate from
#    `signals.postmaster.forces_hard_gate`) had no move, and this item's
#    own "believed harmless... but 'believed' is doing the same
#    load-bearing work item 1 above flags" turned out to be exactly right:
#    the streak dimension (never the status/limit ones) diverged live-vs-
#    replay, because `evaluate()`'s compliance branch skipped the same
#    `clear_unsupported_throttle_streak` call every OTHER PAUSE-producing
#    path made. `COMPLIANCE_PAUSE` is now in `_PERMUTATION_MOVES` below.
#    This is the list doing exactly what it was written to do -- naming a
#    gap precisely enough that finding what was in it was mechanical, not
#    exploratory. CAVEAT (CLOSE9-5): `COMPLIANCE_PAUSE` in the sweep is
#    pinned to `pause_outcome=PERFORMED` only -- the FAILED and UNSUPPORTED
#    compliance-gated outcomes are covered by three hand-written tests
#    (`test_throttle_unsupported_then_compliance_pause_failed_agrees_on_
#    all_three_fields` and neighbours), NOT by the sweep itself. Item 3 is
#    closed for the outcome the sweep actually covers; the other two
#    outcomes are closed by different, narrower evidence.
# 4. MEASURED CLEAN (external audit, CLOSE9-5): the floor-escalation
#    (CLOSE-3d) and unsupported-streak-escalation (CLOSE3-2) paths to
#    PAUSE, swept as their OWN moves rather than only via a fresh ladder
#    PAUSE -- 0/130,321 mismatches at length 4. Same caveat as items 1-2.
# 5. MEASURED CLEAN (external audit, CLOSE9-5): randomized sequences of
#    length 5 to 9 (beyond the main sweep's fixed `repeat=3`) -- 0/200,000
#    sequences. Same caveat: randomized sampling at this volume is strong
#    evidence, not a proof, and worth re-running whenever the state
#    machine's shape changes.
# 6. NEW (CLOSE8-4, from CLOSE8-2). No driver's read-path exception types
#    were ever enumerated against what `cli.main` actually catches, until
#    CLOSE8-2 did it once, by hand, after `SesDriver._daily_sums` already
#    tracebacked in production. CORRECTION (CLOSE9-4): the "the four
#    `httpx`-based drivers turned out fine, `httpx.HTTPError` covers all of
#    them" claim this item originally repeated was ITSELF wrong -- three
#    `httpx` exception families (`InvalidURL`, `CookieConflict`,
#    `StreamError`) are not `httpx.HTTPError` at all. They still land
#    safely in CLOSE8-2's catch-all (exit 3, no traceback), so this was a
#    documentation gap, not a live one -- but it means even THIS item's own
#    correction needed a correction, which is exactly the "verify by
#    execution, not by re-stating the previous claim" discipline this
#    whole list exists to enforce. The next driver added to this project
#    (a tenth surveyed platform, a new client library) needs the same
#    enumeration done for it BEFORE it ships, not after a live account
#    produces the exception nobody thought to catch. See CHANGELOG.md's
#    CLOSE8-2/CLOSE9-4 entries for the table.
# 7. NEW (CLOSE8-4, from CLOSE8-3). The `in`-over-`inspect.getsource`
#    failure shape (a docstring or comment mention alone making an `in`
#    assertion pass, without the code it claims to check actually being
#    there) has now appeared twice, in two different rounds, in two
#    different functions. It is now checked structurally
#    (`tests/test_source_inspect.py`), but CLOSE9-3 found the checker
#    ITSELF didn't catch the exact shape it was built for (a two-statement
#    `source = inspect.getsource(f)` / `source.index(...)` split, and
#    unscanned subdirectories) -- the checker is now `ast`-based and
#    `rglob`-recursive, but the standing question for whenever a future
#    round adds any new source-level guard, of any shape, is still: "does
#    this assertion pass only because the CODE has the property being
#    checked, or could a comment/docstring/unrelated string alone make it
#    pass?" `not in` is safe by construction; `in` needs `source_body` or
#    an equivalent structural check.
# 8. NEW (CLOSE9-5, from CLOSE9-1) -- the headline lesson of this round,
#    and the reason CLOSE9-1 is the most serious defect found across nine
#    rounds. Every sweep in this file, without exception, compares LIVE
#    against REPLAY -- an equivalence question. CLOSE9-1 (one FAILED pause
#    attempt permanently disabling throttling for that mailbox) was
#    invisible to every one of them: the live path and `from_log` agreed
#    with each other at EVERY step, and the daemon and cron call shapes
#    were IDENTICAL. They were simply both stuck in a state neither should
#    have reached. No amount of sweeping wider, deeper, or longer closes
#    this gap, because the gap isn't in coverage -- it's in the QUESTION
#    the sweep asks. An equivalence test can only ever catch two
#    implementations disagreeing; it is structurally incapable of catching
#    a wrong answer both of them give the SAME way. This is the second
#    time that exact limit has produced a real defect (the first was
#    subtler and self-corrected before shipping); CLOSE9-1's own
#    `test_no_bounded_run_of_healthy_evaluations_leaves_a_mailbox_
#    unactionable` is this project's first LIVE-vs-SHOULD test -- a
#    property checked against what's actually correct, not against a
#    second implementation -- and probably shouldn't be its last. Before
#    adding the next sweep dimension, ask: is the thing I'm worried about
#    "these two paths might disagree," or "both paths might quietly agree
#    on something wrong"? Only a live-vs-should test answers the second
#    question.

_PERMUTATION_MOVES = (
    "PAUSE_PERFORMED",
    "PAUSE_UNSUPPORTED",
    "PAUSE_FAILED",
    "THROTTLE_PERFORMED",
    "THROTTLE_UNSUPPORTED",
    "THROTTLE_FAILED",
    "OK",
    "WARN",
    "ZERO_SENDS",
    "COMPLIANCE_PAUSE",
    "RESUME",
)

# CLOSE7-4: `DRY_RUN_PAUSE` is deliberately NOT part of `_PERMUTATION_MOVES`
# above. The main sweep asserts LIVE == REPLAY for every ordering -- but a
# dry-run move breaks that equality ON PURPOSE (CLOSE6-4's own decision):
# the live in-process `state_store` keeps accumulating a dry-run mutation
# across ticks (so a second dry-run evaluation of an already-dry-run-paused
# mailbox correctly reports "already paused," matching what a LIVE daemon
# would decide -- AGENTS.md's "dry-run decisions must be identical to the
# live path"), while `from_log` deliberately skips every dry-run record
# entirely when rebuilding across a restart (CLOSE-4: a dry-run action
# never touched the real provider, so it must never be read back as
# durable history). Folding `DRY_RUN_PAUSE` into `_PERMUTATION_MOVES` would
# make every sequence containing it register as a "mismatch" the main
# sweep has no way to distinguish from a real regression -- it would
# either have to special-case dry-run moves inline (defeating the point of
# one shared, blind comparison) or go permanently red. Tested separately
# below instead, with the actual expected asymmetry spelled out, so this
# is a known, pinned gap rather than one the sweep is silently blind to.
_DRY_RUN_MOVES = ("DRY_RUN_PAUSE",)


def _apply_move(move: str, state_store: BreakerStateStore, log_path: Path) -> None:
    """Apply one move to `state_store` via a real `evaluate()` call (or, for
    RESUME, `resume_after_human_review`), appending exactly the record the
    live path itself would append. Shared verbatim between the live
    reference sequence and the sequence being replayed into the log --
    the only difference between the two is WHEN `from_log` rebuilds from
    those records, never what produced them."""
    if move == "RESUME":
        state_store.resume_after_human_review(_MAILBOX)
        append_resume_record(
            log_path,
            ResumeRecord(
                resumed_at=_NOW,
                provider=_MAILBOX.provider,
                mailbox_id=_MAILBOX.mailbox_id,
                resumed_by="kate",
            ),
        )
        return

    driver_kwargs: dict[str, object] = {}
    eval_kwargs: dict[str, object] = {"sends": 5000, "complaints": 40}
    if move == "PAUSE_PERFORMED":
        driver_kwargs = {"pause_outcome": ActionOutcome.PERFORMED}
        eval_kwargs = {"sends": 5000, "complaints": 40}
    elif move == "PAUSE_UNSUPPORTED":
        driver_kwargs = {"pause_outcome": ActionOutcome.UNSUPPORTED}
        eval_kwargs = {"sends": 5000, "complaints": 40}
    elif move == "PAUSE_FAILED":
        driver_kwargs = {"pause_outcome": ActionOutcome.FAILED}
        eval_kwargs = {"sends": 5000, "complaints": 40}
    elif move == "THROTTLE_PERFORMED":
        driver_kwargs = {"throttle_outcome": ActionOutcome.PERFORMED}
        eval_kwargs = {"sends": 20_000, "complaints": 30, "current_daily_limit": 100}
    elif move == "THROTTLE_UNSUPPORTED":
        eval_kwargs = {"sends": 20_000, "complaints": 30, "current_daily_limit": None}
    elif move == "THROTTLE_FAILED":
        driver_kwargs = {"throttle_outcome": ActionOutcome.FAILED}
        eval_kwargs = {"sends": 20_000, "complaints": 30, "current_daily_limit": 100}
    elif move == "OK":
        eval_kwargs = {"sends": 5000, "complaints": 0}
    elif move == "WARN":
        # Handcrafted to land in the warn band, same as
        # test_warn_verdict_takes_no_provider_action -- WARN never reaches
        # `_act`, so it must leave an already-PAUSED (or any other) status
        # untouched on the live path.
        eval_kwargs = {"sends": 20_000, "complaints": 20}
    elif move == "ZERO_SENDS":
        # CLOSE7-1: `evaluate()`'s `sends == 0` early return -- always
        # Verdict.OK, DataState.INSUFFICIENT_DATA, action=None. This is the
        # move `_apply_move` structurally cannot make ambiguous: `sends=0`
        # forces the early return regardless of `driver_kwargs`, so there's
        # nothing to configure on the driver at all.
        eval_kwargs = {"sends": 0, "complaints": 0}
    elif move == "COMPLIANCE_PAUSE":
        # CLOSE8-1: the hard compliance gate -- always Verdict.PAUSE,
        # regardless of sends/complaints (healthy evidence on purpose, to
        # isolate that it's the GATE forcing PAUSE, not the evidence).
        driver_kwargs = {"pause_outcome": ActionOutcome.PERFORMED}
        eval_kwargs = {"sends": 5000, "complaints": 0, "compliance_gate_tripped": True}
    elif move == "DRY_RUN_PAUSE":
        # CLOSE7-4: same PAUSE-worthy evidence as PAUSE_PERFORMED, but
        # evaluated with `dry_run=True` below -- see `_DRY_RUN_MOVES`'s own
        # comment for why this move is excluded from `_PERMUTATION_MOVES`.
        driver_kwargs = {"pause_outcome": ActionOutcome.PERFORMED}
        eval_kwargs = {"sends": 5000, "complaints": 40}
    else:  # pragma: no cover -- exhaustive over _PERMUTATION_MOVES + _DRY_RUN_MOVES
        raise AssertionError(f"unhandled move {move!r}")

    driver = FakeDriver(**driver_kwargs)  # type: ignore[arg-type]
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=move in _DRY_RUN_MOVES,
        now=_NOW,
        **eval_kwargs,  # type: ignore[arg-type]
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))


def _snapshot(store: BreakerStateStore) -> tuple[MailboxBreakerStatus, int | None, int]:
    """All three fields CLOSE5-2 found `from_log` could disagree with the
    live path on -- comparing `status_of` alone (as this test originally
    did) happened to find nothing extra for the CLOSE4-1 move set, but that
    was luck, not coverage: the stale `throttled_at_limit` CLOSE5-2 fixed
    never showed up as a status mismatch on its own."""
    return (
        store.status_of(_MAILBOX),
        store.throttled_at_limit(_MAILBOX),
        store.unsupported_throttle_streak(_MAILBOX),
    )


@pytest.mark.parametrize("sequence", list(itertools.product(_PERMUTATION_MOVES, repeat=3)))
def test_from_log_replay_matches_the_live_path_over_every_move_ordering(
    sequence: tuple[str, ...], tmp_path: Path
) -> None:
    """The invariant CLOSE4-1's bug violated, tested directly rather than
    via one or two hand-picked sequences: replaying the decision log must
    always reproduce the SAME (status, throttled_at_limit,
    unsupported_throttle_streak) a single uninterrupted in-process run
    through the identical sequence of evaluations would have reached.

    `itertools.product(moves, repeat=3)` rather than `permutations` --
    CLOSE5-2 was found only once repeated moves were covered (`permutations`
    never repeats an element), and 3 moves at a time keeps the sweep at
    1,331 sequences (11 moves -- CLOSE6-2 added `PAUSE_FAILED` and `WARN`,
    CLOSE7-1 added `ZERO_SENDS`, CLOSE8-1 added `COMPLIANCE_PAUSE`) rather
    than needing all 11 in every sequence."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    for move in sequence:
        _apply_move(move, live_store, log_path)

    replayed_store = BreakerStateStore.from_log(log_path)
    assert _snapshot(replayed_store) == _snapshot(live_store), sequence


# --- CLOSE8-1: a compliance-forced PAUSE must clear the streak too --------


def _apply_compliance_pause(
    outcome: ActionOutcome, state_store: BreakerStateStore, log_path: Path
) -> None:
    """A compliance-gate-forced PAUSE with a specific provider outcome --
    healthy evidence on purpose (`sends=5000, complaints=0`), to isolate
    that it's the GATE forcing PAUSE, not the evidence."""
    driver = FakeDriver(pause_outcome=outcome)
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        compliance_gate_tripped=True,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))


def test_throttle_unsupported_then_compliance_pause_performed_agrees_on_all_three_fields(
    tmp_path: Path,
) -> None:
    """CLOSE8-1's first isolated reproduction:
    `THROTTLE_UNSUPPORTED -> COMPLIANCE_PAUSE(PERFORMED)`. Before the fix:
    LIVE=('PAUSED', None, 1), REPLAY=('PAUSED', None, 0) -- the streak
    diverges even though status and limit already agreed."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    _apply_move("THROTTLE_UNSUPPORTED", live_store, log_path)
    assert live_store.unsupported_throttle_streak(_MAILBOX) == 1

    _apply_compliance_pause(ActionOutcome.PERFORMED, live_store, log_path)

    assert _snapshot(live_store) == (MailboxBreakerStatus.PAUSED, None, 0)
    replayed = BreakerStateStore.from_log(log_path)
    assert _snapshot(replayed) == _snapshot(live_store)


def test_throttle_unsupported_then_compliance_pause_failed_agrees_on_all_three_fields(
    tmp_path: Path,
) -> None:
    """The second isolated reproduction:
    `THROTTLE_UNSUPPORTED -> COMPLIANCE_PAUSE(FAILED)`. Before the fix:
    LIVE=('PAUSE_FAILED', None, 1), REPLAY=('PAUSE_FAILED', None, 0)."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    _apply_move("THROTTLE_UNSUPPORTED", live_store, log_path)
    assert live_store.unsupported_throttle_streak(_MAILBOX) == 1

    _apply_compliance_pause(ActionOutcome.FAILED, live_store, log_path)

    assert _snapshot(live_store) == (MailboxBreakerStatus.PAUSE_FAILED, None, 0)
    replayed = BreakerStateStore.from_log(log_path)
    assert _snapshot(replayed) == _snapshot(live_store)


def test_repeated_throttle_unsupported_around_a_compliance_pause_agrees(tmp_path: Path) -> None:
    """The third isolated reproduction, the one that shows this is
    consequential rather than cosmetic:
    `THROTTLE_UNSUPPORTED x2 -> COMPLIANCE_PAUSE(FAILED) -> THROTTLE_
    UNSUPPORTED`. Before the fix, the compliance PAUSE left the LIVE
    streak at 2 (untouched), so the trailing `THROTTLE_UNSUPPORTED` took it
    to 3 -- `_MAX_UNSUPPORTED_THROTTLE_STREAK` -- while REPLAY (which always
    popped the streak for any PAUSE record) reached only 1: LIVE=
    ('PAUSE_FAILED', None, 3), REPLAY=('PAUSE_FAILED', None, 1). A live
    streak sitting AT the escalation threshold while the replayed one isn't
    is what makes the NEXT evaluation on each path take a genuinely
    different action (see
    `test_compliance_pause_daemon_and_cron_escalate_the_same_way` below).
    After the fix, the compliance PAUSE clears the live streak too, so both
    paths agree at 1 -- checked here, not 3."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    _apply_move("THROTTLE_UNSUPPORTED", live_store, log_path)
    _apply_move("THROTTLE_UNSUPPORTED", live_store, log_path)
    assert live_store.unsupported_throttle_streak(_MAILBOX) == 2

    _apply_compliance_pause(ActionOutcome.FAILED, live_store, log_path)
    assert live_store.unsupported_throttle_streak(_MAILBOX) == 0  # cleared by the fix

    _apply_move("THROTTLE_UNSUPPORTED", live_store, log_path)

    assert _snapshot(live_store) == (MailboxBreakerStatus.PAUSE_FAILED, None, 1)
    replayed = BreakerStateStore.from_log(log_path)
    assert _snapshot(replayed) == _snapshot(live_store)


def test_compliance_pause_daemon_and_cron_escalate_the_same_way(tmp_path: Path) -> None:
    """The behavioural consequence, daemon vs cron, on the exact prefix
    from `test_repeated_throttle_unsupported_around_a_compliance_pause_
    agrees` above: two `THROTTLE_UNSUPPORTED`, a `COMPLIANCE_PAUSE(FAILED)`,
    a third `THROTTLE_UNSUPPORTED` -- reaching a streak of 3, exactly
    `_MAX_UNSUPPORTED_THROTTLE_STREAK`. A FOURTH evaluation, still
    THROTTLE-worthy evidence with `current_daily_limit` still unknown, must
    escalate to a real PAUSE either way. Before the fix: the daemon (one
    live `state_store`, streak genuinely at 3) escalates and calls
    `driver.pause()` for real; the cron form (`state_store` rebuilt via
    `from_log` between every evaluation, streak reset to 1 by the
    compliance record) computes THROTTLE/UNSUPPORTED instead and never
    calls the provider at all -- the exact "daemon acts for real, cron
    doesn't" shape CLOSE3-2/CLOSE4-1/CLOSE5-2 each closed for a
    neighbouring path."""

    def _run_prefix_plus_fourth_evaluation(
        state_store: BreakerStateStore, log_path: Path, *, restart_between_evaluations: bool
    ) -> FakeDriver:
        # Shared across all evaluations. `pause_outcome=FAILED` keeps the
        # mailbox OUT of PAUSED after the compliance step -- if it
        # PERFORMED (FakeDriver's default), `_act`'s THROTTLE branch would
        # short-circuit as paused-idempotent for every evaluation after
        # (CLOSE5-1), which would mask the streak-escalation question this
        # test exists to ask.
        driver = FakeDriver(pause_outcome=ActionOutcome.FAILED)
        for _ in range(2):  # the 1st and 2nd THROTTLE_UNSUPPORTED evaluations
            evaluation = evaluate(
                driver=driver,
                mailbox=_MAILBOX,
                sends=20_000,
                complaints=30,
                current_daily_limit=None,
                prior=DEFAULT_PRIOR,
                thresholds=DEFAULT_LADDER,
                state_store=state_store,
                dry_run=False,
                now=_NOW,
            )
            append_record(log_path, DecisionRecord.from_evaluation(evaluation))
            if restart_between_evaluations:
                state_store = BreakerStateStore.from_log(log_path)

        compliance_eval = evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=5000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=state_store,
            dry_run=False,
            now=_NOW,
            compliance_gate_tripped=True,
        )
        append_record(log_path, DecisionRecord.from_evaluation(compliance_eval))
        if restart_between_evaluations:
            state_store = BreakerStateStore.from_log(log_path)

        for _ in range(2):  # the 3rd and the escalating 4th THROTTLE_UNSUPPORTED evaluation
            evaluation = evaluate(
                driver=driver,
                mailbox=_MAILBOX,
                sends=20_000,
                complaints=30,
                current_daily_limit=None,
                prior=DEFAULT_PRIOR,
                thresholds=DEFAULT_LADDER,
                state_store=state_store,
                dry_run=False,
                now=_NOW,
            )
            append_record(log_path, DecisionRecord.from_evaluation(evaluation))
            if restart_between_evaluations:
                state_store = BreakerStateStore.from_log(log_path)

        return driver

    daemon_store = BreakerStateStore()
    daemon_driver = _run_prefix_plus_fourth_evaluation(
        daemon_store, tmp_path / "daemon.jsonl", restart_between_evaluations=False
    )

    cron_driver = _run_prefix_plus_fourth_evaluation(
        BreakerStateStore(), tmp_path / "cron.jsonl", restart_between_evaluations=True
    )

    assert daemon_driver.pause_calls == cron_driver.pause_calls
    assert daemon_driver.throttle_calls == cron_driver.throttle_calls

    final_daemon = BreakerStateStore.from_log(tmp_path / "daemon.jsonl")
    final_cron = BreakerStateStore.from_log(tmp_path / "cron.jsonl")
    assert final_daemon.status_of(_MAILBOX) == final_cron.status_of(_MAILBOX)


# --- CLOSE9-1: a live-vs-SHOULD property, not a live-vs-replay one --------
#
# Every sweep above (and CLOSE9-1's own reproduction) compares the LIVE
# path against REPLAY, or one calling shape against another -- an
# equivalence question. CLOSE9-1 itself was invisible to all of them: the
# live path and `from_log` agreed with each other at every step, and the
# daemon and cron forms made identical calls. They were just both stuck.
# No equivalence test can find a wrong answer both sides give the same
# way -- only a test that asks "is this actually right," independent of
# any second implementation to compare against, can. This is that test.


@pytest.mark.parametrize("starting_move", [m for m in _PERMUTATION_MOVES if m != "RESUME"])
def test_no_bounded_run_of_healthy_evaluations_leaves_a_mailbox_unactionable(
    starting_move: str, tmp_path: Path
) -> None:
    """After ANY starting move, followed by a bounded run of genuinely
    healthy (`Verdict.OK`, real evidence) evaluations, the mailbox must
    end up somewhere a provider action is still reachable: `ACTIVE`
    (sustained recovery fired, or nothing ever happened), or `PAUSED`
    (ADR 0003's OWN intentional permanent state -- a human can always
    `resume` it, so this is not "stuck," it's the policy). Anything else
    -- `THROTTLED` or `PAUSE_FAILED` surviving thirty straight healthy
    days -- means sustained recovery silently failed to fire for a real
    reason to recover, which is exactly CLOSE9-1's shape. `PAUSE_IN_FLIGHT`
    is not reachable here: `_apply_move`'s FakeDriver-backed moves always
    resolve within the same `evaluate()` call, the same as every real
    driver's `pause()` either returning or raising, never hanging."""
    log_path = tmp_path / "decisions.jsonl"
    state_store = BreakerStateStore()
    _apply_move(starting_move, state_store, log_path)

    for _ in range(30):
        _apply_move("OK", state_store, log_path)

    status = state_store.status_of(_MAILBOX)
    assert status in (MailboxBreakerStatus.ACTIVE, MailboxBreakerStatus.PAUSED), (
        f"after {starting_move!r} then 30 healthy evaluations, mailbox is stuck "
        f"in {status.name} -- a provider action should still be reachable, either "
        f"through sustained recovery or (for PAUSED specifically) a human resume"
    )


# --- CLOSE7-4: the dry-run asymmetry, pinned rather than left as a gap ----


@pytest.mark.parametrize(
    "sequence",
    [(a, "DRY_RUN_PAUSE", b) for a in _PERMUTATION_MOVES for b in _PERMUTATION_MOVES],
)
def test_from_log_replay_is_unaffected_by_a_dry_run_move_anywhere_in_the_sequence(
    sequence: tuple[str, str, str], tmp_path: Path
) -> None:
    """The half of the asymmetry that IS a universal invariant, regardless
    of where the dry-run move sits or what surrounds it: `from_log` skips
    every dry-run record entirely (CLOSE-4), so replaying a log must equal
    replaying that SAME log with its dry-run record(s) deleted afterward.
    This is what "the replayed store does not accumulate" actually means,
    made explicit and swept over every surrounding pair rather than
    asserted for one hand-picked example.

    Deliberately NOT "re-run the sequence with the dry-run move skipped":
    `_apply_move`'s later calls consult `state_store.status_of` (e.g.
    `PAUSE_UNSUPPORTED`'s outcome depends on whether the mailbox is
    already PAUSED), and a dry-run move DOES mutate the live in-process
    store (CLOSE6-4's own decision) -- so skipping it changes what the
    LATER moves in the sequence themselves decide to do, which is a
    different question from what THIS RECORD contributes to replay. Only
    comparing against the same already-generated log with the dry-run
    record removed isolates that."""
    from deliverability_guard.audit.log import read_events

    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    for move in sequence:
        _apply_move(move, live_store, log_path)
    replayed_with_dry_run = BreakerStateStore.from_log(log_path)

    log_path_without = tmp_path / "decisions_without_dry_run.jsonl"
    for event in read_events(log_path):
        if isinstance(event, DecisionRecord) and event.dry_run:
            continue
        if isinstance(event, DecisionRecord):
            append_record(log_path_without, event)
        else:
            append_resume_record(log_path_without, event)
    replayed_without_dry_run = (
        BreakerStateStore.from_log(log_path_without)
        if log_path_without.exists()
        else BreakerStateStore()
    )

    assert _snapshot(replayed_with_dry_run) == _snapshot(replayed_without_dry_run), sequence


def test_dry_run_pause_accumulates_live_but_not_in_replay(tmp_path: Path) -> None:
    """The other half: a single explicit worked example of the live side
    actually accumulating, which the sweep above deliberately does not
    (and structurally cannot, without asserting "must differ" -- not
    always true, e.g. when the mailbox is already PAUSED for an unrelated
    reason and the dry-run pause is itself an idempotent no-op) claim as a
    universal property. From ACTIVE: a `DRY_RUN_PAUSE` moves the LIVE
    in-process store to PAUSED (matching what a live daemon's second tick
    would report -- "already paused" -- on a THIRD dry-run evaluation),
    while replaying the very same record leaves the mailbox ACTIVE,
    because `from_log` never touched it at all."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()

    _apply_move("DRY_RUN_PAUSE", live_store, log_path)

    assert live_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED

    replayed = BreakerStateStore.from_log(log_path)
    assert replayed.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


# --- CLOSE7-1: a zero-send day is silence, never a recovery --------------


def test_from_log_a_zero_send_day_after_throttle_does_not_clear_the_status_or_limit(
    tmp_path: Path,
) -> None:
    """The isolated two-move reproduction: THROTTLE/PERFORMED, then a
    zero-send day. `evaluate()`'s own `sends == 0` early return is
    `Verdict.OK` + `DataState.INSUFFICIENT_DATA` -- CLOSE3-3's recovery
    branch used to fire on `Verdict.OK` alone, reading the silence as
    "the mailbox recovered" and clearing both the THROTTLED status and the
    remembered limit."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    _apply_move("THROTTLE_PERFORMED", live_store, log_path)
    assert live_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    assert live_store.throttled_at_limit(_MAILBOX) == 100

    _apply_move("ZERO_SENDS", live_store, log_path)

    # The live path itself takes no action on a zero-send day -- nothing
    # about the mailbox's state changes.
    assert live_store.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    assert live_store.throttled_at_limit(_MAILBOX) == 100

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    assert restored.throttled_at_limit(_MAILBOX) == 100


def test_from_log_insufficient_data_never_clears_throttled_at_limit(tmp_path: Path) -> None:
    """The DoD's explicit assertion, isolated from status: even if some
    future change made status-tracking more permissive, `throttled_at_
    limit` -- the CLOSE3-1 idempotency key that's the entire point of this
    finding -- must never be cleared by a record carrying no evidence."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    _apply_move("THROTTLE_PERFORMED", live_store, log_path)
    _apply_move("ZERO_SENDS", live_store, log_path)
    _apply_move("ZERO_SENDS", live_store, log_path)

    restored = BreakerStateStore.from_log(log_path)
    assert restored.throttled_at_limit(_MAILBOX) is not None
    assert restored.throttled_at_limit(_MAILBOX) == 100


def test_from_log_zero_sends_after_unsupported_throttle_does_not_reset_the_streak(
    tmp_path: Path,
) -> None:
    """The second divergence the isolated reproduction actually found
    (LIVE ('ACTIVE', None, 1) vs REPLAY ('ACTIVE', None, 0)): `evaluate()`'s
    `sends == 0` early return returns before reaching the "verdict is not
    THROTTLE -> clear the unsupported-throttle streak" line every other
    non-THROTTLE verdict hits, so the streak must survive a zero-send day
    too, not just status/limit."""
    log_path = tmp_path / "decisions.jsonl"
    live_store = BreakerStateStore()
    _apply_move("THROTTLE_UNSUPPORTED", live_store, log_path)
    assert live_store.unsupported_throttle_streak(_MAILBOX) == 1

    _apply_move("ZERO_SENDS", live_store, log_path)
    assert live_store.unsupported_throttle_streak(_MAILBOX) == 1

    restored = BreakerStateStore.from_log(log_path)
    assert restored.unsupported_throttle_streak(_MAILBOX) == 1


class _AlternatingZeroSendDriver:
    """Reports a real, shrinking `current_daily_limit` and alternates one
    zero-send day between bad days -- the CLOSE7-1 end-to-end
    reproduction. `phase` is advanced externally, one call per evaluation,
    matching the shape `_SmartleadShapedDriver` in `tests/test_cli.py`
    already established for CLOSE5-2's daemon-vs-cron comparison."""

    name = "fake"
    capabilities = frozenset({Capability.READ_STATS, Capability.THROTTLE, Capability.PAUSE})

    def __init__(self) -> None:
        self.current_daily_limit = 100
        self.throttle_calls: list[tuple[str, int]] = []
        self.pause_calls: list[MailboxRef | CampaignRef] = []
        self.phase = 0  # 0 = bad day, 1 = zero-send day; alternates

    def read_mailbox_stats(
        self, since: date
    ) -> list[MailboxDayStats]:  # pragma: no cover -- unused
        raise AssertionError("stats are read directly by the test, not through this method")

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        self.throttle_calls.append((mailbox_id, daily_limit))
        self.current_daily_limit = daily_limit
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail="fake: throttled",
            capability=Capability.THROTTLE,
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        self.pause_calls.append(target)
        return ActionResult(
            outcome=ActionOutcome.PERFORMED, detail="fake: paused", capability=Capability.PAUSE
        )


def _alternating_zero_send_evaluation(
    driver: _AlternatingZeroSendDriver, state_store: BreakerStateStore
) -> BreakerEvaluation:
    """One evaluation of the CLOSE7-1 end-to-end scenario: a bad day
    (same evidence as `_apply_move`'s `THROTTLE_PERFORMED` -- enough to
    land in the THROTTLE band, not straight to PAUSE) on even calls, a
    zero-send day on odd calls -- against whatever `current_daily_limit`
    the driver currently remembers, exactly like a real provider report
    would."""
    if driver.phase % 2 == 0:
        sends, complaints = 20_000, 30
    else:
        sends, complaints = 0, 0
    result = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=sends,
        complaints=complaints,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=driver.current_daily_limit,
    )
    driver.phase += 1
    return result


def test_cron_and_daemon_diverge_on_alternating_zero_send_days_before_the_fix_agree_after(
    tmp_path: Path,
) -> None:
    """CLOSE7-1's end-to-end reproduction: identical evidence -- a bad day,
    a zero-send day, a bad day, a zero-send day, ... -- against a driver
    reporting its REAL shrinking daily limit, run once as an uninterrupted
    daemon (one `state_store`, twenty ticks, no restarts) and once as
    twenty separate `check`-shaped processes (`from_log` rebuilt fresh
    every time). Both must make the SAME real provider calls and reach the
    SAME final state. Before this fix, cron's zero-send days each read as
    a fresh recovery, so cron kept re-throttling from a mailbox `from_log`
    incorrectly believed was ACTIVE (100 -> 50 -> 25 -> 12 -> 6 -> 3 -> an
    unearned PAUSE), while a daemon covering the identical history throttles
    once and stops, because its second bad day is genuinely still THROTTLED
    at 50 -- exactly CLOSE3-1's compounding failure, resurfaced through a
    door the permutation sweep couldn't reach until `ZERO_SENDS` existed."""
    daemon_driver = _AlternatingZeroSendDriver()
    daemon_store = BreakerStateStore()
    for _ in range(20):
        _alternating_zero_send_evaluation(daemon_driver, daemon_store)

    cron_driver = _AlternatingZeroSendDriver()
    cron_log = tmp_path / "decisions.jsonl"
    cron_store = BreakerStateStore()
    for _ in range(20):
        evaluation = _alternating_zero_send_evaluation(cron_driver, cron_store)
        append_record(cron_log, DecisionRecord.from_evaluation(evaluation))
        # Restart: rebuild state from the log alone, exactly like a fresh
        # `check` process would, discarding the in-process `cron_store`.
        cron_store = BreakerStateStore.from_log(cron_log)

    assert cron_driver.throttle_calls == daemon_driver.throttle_calls
    assert cron_driver.pause_calls == daemon_driver.pause_calls
    assert cron_store.status_of(_MAILBOX) == daemon_store.status_of(_MAILBOX)
    assert cron_store.throttled_at_limit(_MAILBOX) == daemon_store.throttled_at_limit(_MAILBOX)


# --- CLOSE6-2: a hand-edited/merged/restored log must not un-pause -------
#
# The live path itself can NEVER produce a PAUSE/UNSUPPORTED or
# PAUSE/FAILED record for an already-PAUSED mailbox: `_act`'s PAUSE branch
# short-circuits to an idempotent PERFORMED result before ever calling
# `driver.pause()` again once a mailbox is PAUSED, so `_apply_move` above
# (which always goes through a real `evaluate()` call) structurally cannot
# reach this sequence -- these tests build the log directly instead, the
# same way a hand-edited, merged, or restored-from-backup JSONL file
# would.


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_a_forged_unsupported_record(
    tmp_path: Path,
) -> None:
    """CLOSE6-2: `from_log`'s PAUSE/`UNSUPPORTED` branch used to set status
    unconditionally, with no already-PAUSED check -- unlike `_act`'s own
    live-path branch, which can never reach an UNSUPPORTED outcome once a
    mailbox is already PAUSED at all. A log containing
    [PAUSE/PERFORMED, PAUSE/UNSUPPORTED] for the same mailbox is not
    something the live path can write, but replaying one un-paused the
    mailbox anyway, with no `ResumeRecord` anywhere in the log."""
    log_path = tmp_path / "decisions.jsonl"
    pause_eval = evaluate(
        driver=FakeDriver(pause_outcome=ActionOutcome.PERFORMED),
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    assert pause_eval.verdict is Verdict.PAUSE
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))

    forged_unsupported = dataclasses.replace(
        DecisionRecord.from_evaluation(pause_eval),
        action_outcome=ActionOutcome.UNSUPPORTED,
        action_detail="forged: pretends this provider can never pause this target",
    )
    append_record(log_path, forged_unsupported)

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_rebuild_keeps_a_paused_mailbox_paused_through_a_forged_failed_record(
    tmp_path: Path,
) -> None:
    """Same defect shape, the neighbouring branch: `from_log`'s PAUSE/
    `FAILED` branch had the same missing guard."""
    log_path = tmp_path / "decisions.jsonl"
    pause_eval = evaluate(
        driver=FakeDriver(pause_outcome=ActionOutcome.PERFORMED),
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    assert pause_eval.verdict is Verdict.PAUSE
    append_record(log_path, DecisionRecord.from_evaluation(pause_eval))

    forged_failed = dataclasses.replace(
        DecisionRecord.from_evaluation(pause_eval),
        action_outcome=ActionOutcome.FAILED,
        action_detail="forged: pretends a second pause attempt failed",
    )
    append_record(log_path, forged_failed)

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED
    # Not PAUSE_FAILED either -- the guard leaves status untouched entirely,
    # it doesn't substitute a different automatic transition.
    assert restored.status_of(_MAILBOX) is not MailboxBreakerStatus.PAUSE_FAILED


@pytest.mark.parametrize("move", [m for m in _PERMUTATION_MOVES if m != "RESUME"])
def test_only_resume_after_human_review_moves_paused_back_to_active_exclusivity(
    move: str, tmp_path: Path
) -> None:
    """CLOSE6-2: `test_only_resume_after_human_review_moves_paused_back_to_
    active` (above, in the `BreakerStateStore: never auto-resume` section)
    is NAMED for an exclusivity claim -- "only" resume moves PAUSED back to
    ACTIVE -- but its body only ever asserted that resume works, never that
    every OTHER move fails to. This is the other half: starting from
    PAUSED, every move in `_PERMUTATION_MOVES` except RESUME must leave the
    mailbox PAUSED, driven entirely through the live path (`_apply_move`,
    the same helper the permutation sweep uses) so this is a genuine
    behavioral guarantee, not a source-level grep."""
    log_path = tmp_path / "decisions.jsonl"
    state_store = BreakerStateStore()
    _apply_move("PAUSE_PERFORMED", state_store, log_path)
    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED

    _apply_move(move, state_store, log_path)

    assert state_store.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_rebuild_reverts_to_active_on_an_unsupported_pause_attempt(
    tmp_path: Path,
) -> None:
    """A definitively UNSUPPORTED pause -- the provider can never pause this
    target -- rebuilds as ACTIVE, distinct from PAUSE_FAILED (which is for a
    transient FAILED outcome, not a structural one)."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver(capabilities=frozenset({Capability.READ_STATS}))
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_from_log_recovers_a_throttled_mailbox_after_five_healthy_restarts(
    tmp_path: Path,
) -> None:
    """CLOSE3-3: a THROTTLE followed by sustained healthy evaluations must
    clear the persisted THROTTLED status, not just the in-process one --
    `from_log` leaving OK verdicts alone (as it did before this fix) meant a
    fully recovered mailbox that happened to restart mid-recovery read
    THROTTLED forever, and no command could clear it. Five SEPARATE
    restarts (a fresh `BreakerStateStore.from_log` each time, per the
    session rule that every state-related test gets a sibling that
    restarts), not five in-process ticks."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    throttle_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
        current_daily_limit=100,
    )
    append_record(log_path, DecisionRecord.from_evaluation(throttle_eval))
    assert (
        BreakerStateStore.from_log(log_path).status_of(_MAILBOX) == MailboxBreakerStatus.THROTTLED
    )

    for _ in range(5):
        restarted_store = BreakerStateStore.from_log(log_path)
        ok_eval = evaluate(
            driver=driver,
            mailbox=_MAILBOX,
            sends=20_000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=restarted_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=50,
        )
        assert ok_eval.verdict == Verdict.OK
        append_record(log_path, DecisionRecord.from_evaluation(ok_eval))

    final = BreakerStateStore.from_log(log_path)
    assert final.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert final.throttled_at_limit(_MAILBOX) is None


def test_state_store_from_log_with_no_file_yet_is_an_empty_active_store(tmp_path: Path) -> None:
    """No log file at all is a genuinely new deployment -- every mailbox
    correctly defaults to ACTIVE. This is the "no record" case, distinct
    from "couldn't read the record" below."""
    missing = tmp_path / "does-not-exist.jsonl"
    store = BreakerStateStore.from_log(missing)
    assert store.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_from_log_fails_closed_on_an_unreadable_log(tmp_path: Path) -> None:
    """A log that EXISTS but can't be parsed must not silently produce an
    empty (every-mailbox-ACTIVE) store -- that would be indistinguishable
    from "no record" and could auto-resume a mailbox that was actually
    PAUSED. Fail loudly instead."""
    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text("not valid json\n")
    with pytest.raises(BreakerStateStoreLoadError):
        BreakerStateStore.from_log(log_path)


# --- CLOSE-4: resume durability, dry-run non-persistence, empty log --------


def test_state_store_rebuilds_active_status_from_a_resume_record_after_pause(
    tmp_path: Path,
) -> None:
    """The CLOSE-4 reproduction, at the breaker level: a paused mailbox with
    a `ResumeRecord` after it in the log must rebuild as ACTIVE."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    state_store = BreakerStateStore()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=state_store,
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))
    append_resume_record(
        log_path,
        ResumeRecord(
            resumed_at=_NOW, provider="fake", mailbox_id="a@example.com", resumed_by="kate"
        ),
    )

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_rebuild_re_pauses_after_a_later_resume_if_evaluated_again(
    tmp_path: Path,
) -> None:
    """A resume is a point-in-time event, not a permanent exemption: a
    mailbox resumed and then paused AGAIN later must rebuild as PAUSED --
    `from_log` must apply events strictly in file order, not just check
    "was there ever a resume record."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    first_pause = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(first_pause))
    append_resume_record(
        log_path,
        ResumeRecord(
            resumed_at=_NOW, provider="fake", mailbox_id="a@example.com", resumed_by="kate"
        ),
    )
    second_pause = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(second_pause))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.PAUSED


def test_state_store_from_log_skips_a_dry_run_pause_record(tmp_path: Path) -> None:
    """CLOSE-4b's reproduction: a dry-run PAUSE record must never rebuild as
    PAUSED -- the real (fake) driver was never touched."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    evaluation = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert evaluation.verdict == Verdict.PAUSE
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE


def test_state_store_from_log_dry_run_pause_then_real_pause_of_another_mailbox(
    tmp_path: Path,
) -> None:
    """A dry-run pause of one mailbox must not affect a real pause of a
    different mailbox recorded in the same log."""
    log_path = tmp_path / "decisions.jsonl"
    driver = FakeDriver()
    other_mailbox = MailboxRef(provider="fake", mailbox_id="b@example.com")
    dry_eval = evaluate(
        driver=driver,
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(dry_eval))
    real_eval = evaluate(
        driver=driver,
        mailbox=other_mailbox,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    append_record(log_path, DecisionRecord.from_evaluation(real_eval))

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(_MAILBOX) == MailboxBreakerStatus.ACTIVE
    assert restored.status_of(other_mailbox) == MailboxBreakerStatus.PAUSED


def test_state_store_from_log_fails_closed_on_an_empty_log(tmp_path: Path) -> None:
    """A log that EXISTS but is zero bytes is ambiguous between "nothing has
    ever happened" and "the log was truncated" -- fail loudly, the same as
    any other unreadable log, rather than rebuilding an all-ACTIVE store."""
    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text("")
    with pytest.raises(BreakerStateStoreLoadError):
        BreakerStateStore.from_log(log_path)
