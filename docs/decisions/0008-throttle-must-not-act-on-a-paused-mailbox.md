---
status: "accepted"
date: "2026-09-01"
deciders: "Kate Kruger"
---

# `_act`'s THROTTLE branch must refuse a PAUSED mailbox the same way its PAUSE branch already refuses a re-pause

## Context and Problem Statement

`engine.breaker.evaluate` never consults `state_store.status_of(mailbox)`
before computing a verdict, and neither did `_act`'s THROTTLE branch (its
PAUSE branch always has: "a previous trip already got this mailbox paused
... don't call pause() again"). A mailbox already PAUSED -- behind ADR
0003's human-review gate -- whose evidence on a later tick happened to land
in the THROTTLE band rather than the PAUSE band got a REAL `driver.
throttle()` call, and `mark_throttled` moved its persisted status to
THROTTLED. One later OK evaluation then hit CLOSE-3b's sustained-recovery
path (a genuine, wanted transition for a THROTTLED mailbox) and moved it
all the way to ACTIVE -- with `resume_after_human_review` never called and
no `ResumeRecord` ever written to the log (CLOSE5-1, external audit, round
5).

Reproduction, seven separate `check` processes, `dry_run: false`:

```
phase 1 - provider reports no daily limit
  runs 1-4:  THROTTLE, THROTTLE, THROTTLE, PAUSE     -> status PAUSED
phase 2 - provider now reports current_daily_limit=50
  runs 5-6:  THROTTLE, THROTTLE                      -> status THROTTLED
phase 3 - evidence recovers
  run 7:     OK                                      -> status ACTIVE
```

The breaker paused the mailbox, then told the provider to set that same
mailbox's daily limit to 25 -- re-enabling sending -- and three runs later
reported it ACTIVE. This contradicts ADR 0003 itself, `BreakerStateStore`'s
class docstring ("`resume_after_human_review` is the ONLY path back from
PAUSED to ACTIVE"), and `mark_active`'s own docstring ("never for
un-pausing a confirmed-paused mailbox on its own").

## Decision Drivers

- ADR 0003's guarantee must hold for EVERY code path that can act on a
  mailbox, not just the one (PAUSE) that happens to already check status.
  A guarantee that holds for one verb and not another is not the guarantee
  it claims to be.
- The fix must not disturb CLOSE-3b's sustained-recovery path (OK after
  THROTTLED -> ACTIVE), which is a genuine, wanted, already-correct
  transition -- only the NEW, wrong path (PAUSED -> THROTTLED -> ACTIVE)
  that this decision closes.
- Whatever the fix is, it should be checkable by the same crude
  source-level pattern this repo already uses for the symmetric claim about
  `resume_after_human_review` (`tests/test_breaker.py::
  test_evaluate_never_calls_resume_after_human_review`) -- a guarantee this
  easy to check should be checked the same way every time, not
  re-invented per finding.

## Considered Options

- Short-circuit inside `_act`'s THROTTLE branch: check
  `state_store.status_of(mailbox) is PAUSED` first, and return an
  idempotent no-op if so -- the same shape its PAUSE branch already has.
- Short-circuit inside `evaluate()`, before descending the ladder at all,
  for any mailbox already PAUSED.

## Decision Outcome

Chosen option: **short-circuit inside `_act`'s THROTTLE branch.** It is the
smaller change, and it puts the guarantee in the one function that actually
calls the provider -- symmetric with the PAUSE branch's own check forty
lines below, which already lives there for exactly the same reason. The
`evaluate()`-level option was rejected because `evaluate()`'s job is
computing what the EVIDENCE says (the verdict), which is a genuinely
separate question from whether an ACTION is permitted; blocking descent
into the ladder would also require re-deriving, inside `evaluate()`, the
same distinction `_act` already has to make PER VERDICT (a PAUSED mailbox's
OK verdict must still be allowed to flow through unchanged, so CLOSE-3b's
own status check keeps working) -- duplicating logic `_act` was always
going to need anyway.

The returned `ActionResult` uses `outcome=ActionOutcome.PERFORMED` with a
detail naming the paused state, matching the PAUSE branch's own idempotent-
repeat wording ("mailbox already paused; no action taken (idempotent)")
rather than `UNSUPPORTED`: this is not the provider refusing anything, it
is this breaker refusing to ask.

`BreakerStateStore.from_log`'s replay of a THROTTLE/PERFORMED record was
extended to match: if the mailbox's status entering that record (from
records replayed so far) is already PAUSED, the record can ONLY be this
new paused-idempotent no-op -- a genuine throttle can no longer be recorded
for a mailbox that was already PAUSED at evaluation time -- so replay
leaves status and `throttled_at_limit` untouched rather than applying
CLOSE4-1's limit-comparison heuristic (which assumed no PAUSED-mailbox case
existed at all).

### Consequences

- Good, because the seven-phase reproduction above now ends PAUSED with
  zero `throttle()` calls, at every step, across restarts.
- Good, because the fix is symmetric with, and sits next to, the pattern
  that already worked for PAUSE -- a future reader auditing `_act` sees
  both branches check status the same way, rather than one checking and one
  not.
- Good, because CLOSE-3b's OK-after-THROTTLED recovery is untouched: the
  new check is scoped to THROTTLE verdicts reaching `_act`, and the
  recovery check in `evaluate()` was already, and remains, scoped to
  `status is THROTTLED` specifically, never PAUSED.
- Bad, because the invariant now lives in two places that must be kept in
  sync by hand (`_act`'s THROTTLE branch, and `from_log`'s THROTTLE/
  PERFORMED replay branch) rather than one -- the same shape of risk
  CLOSE4-1/CLOSE5-1 both actually manifested as. The permutation test
  (`test_from_log_replay_matches_the_live_path_over_every_move_ordering`)
  exists specifically to catch the next drift between them.

### Confirmation

`tests/test_breaker.py::test_a_paused_mailbox_that_evaluates_to_throttle_is_not_throttled`,
`test_a_paused_mailbox_with_an_ok_verdict_stays_paused`, and
`test_act_checks_paused_status_before_ever_calling_throttle` (the
crude source-level guard, in the same idiom as
`test_evaluate_never_calls_resume_after_human_review`) confirm this
directly. `tests/test_cli.py::
test_seven_phase_reproduction_a_paused_mailbox_never_auto_un_pauses` and
`test_seven_phase_reproduction_never_writes_a_resume_record` reproduce the
finding through the real CLI across separate processes.

## Assumption this relies on

That `state_store.status_of(mailbox)` is always up to date at the moment
`_act` is called -- true by construction, since `evaluate()` never mutates
`state_store` between reading a mailbox's stats and calling `_act` for it.

## Known limitation

This closes the THROTTLE path specifically. Any FUTURE verb this project
adds that can act on a mailbox (there are currently only two: pause and
throttle) will need its own status check written by hand, in the same
place, by whoever adds it -- there is no structural mechanism in `_act`
that forces a new branch to consult `status_of` before acting. The
source-level guard test above checks the two verbs that exist today; it
does not and cannot enforce this for a verb that doesn't exist yet.

## Pros and Cons of the Options

### Short-circuit inside `_act`'s THROTTLE branch (chosen)

- Good, because it is symmetric with the existing PAUSE branch, in the same
  function, checked the same way
- Good, because it is the smallest change that closes the finding
- Bad, because the guarantee still has to be independently re-derived in
  `from_log`'s replay logic, which is a second place the same invariant can
  drift out of sync

### Short-circuit inside `evaluate()` before descending the ladder

- Good, because it would block ALL actions on a PAUSED mailbox from one
  place, rather than one action type at a time
- Bad, because "compute the verdict" and "is an action permitted" are
  different questions, and blocking descent risks either duplicating
  `_act`'s own per-verdict logic or accidentally blocking the wanted
  OK-after-PAUSED path (which must still flow through to confirm nothing
  needs to happen, not be intercepted before it can)

## More Information

See ADR 0003 for the original never-auto-resume guarantee this decision
protects, and CLOSE4-1's fix in `BreakerStateStore.from_log` for the
sibling defect this one's reproduction chain built directly on top of (a
mailbox reaching THROTTLED that should never have left PAUSED in the first
place).
