---
status: "accepted"
date: "2026-08-29"
deciders: "Kate Kruger"
---

# Never auto-resume a paused mailbox

## Context and Problem Statement

Once the breaker pauses a mailbox, its underlying condition can genuinely
improve -- the list gets cleaned, the copy gets fixed, whatever caused the
complaint spike stops. Should the breaker itself notice that improvement and
resume sending automatically, the mirror image of how it paused in the
first place?

## Decision Drivers

- A tool that stops someone's revenue by accident is worse than no tool
  (AGENTS.md) -- but a tool that silently RESTARTS someone's revenue-risking
  behavior without a human looking at it first is arguably worse still: the
  first failure mode is "annoying," the second is "the thing this project
  exists to prevent, self-inflicted."
- The posterior that trips PAUSE is conservative by design (BUILD-PLAN.md
  §6) -- it only fires when there's real confidence of an elevated rate. The
  reverse isn't symmetric: a posterior no longer breaching PAUSE doesn't
  mean the underlying problem (bad list, bad copy, warmup violation) was
  actually fixed, only that recent evidence has been quieter. Sends are
  usually near-zero while paused, so "the posterior looks fine now" is often
  just `INSUFFICIENT_DATA` wearing a mask.
- Complaint data lags 24h-3 days (BUILD-PLAN.md §5). A posterior computed
  from the handful of sends right after an auto-resume would be exactly the
  low-volume, low-confidence situation this whole project exists to distrust
  -- auto-resuming on it would be trusting the very kind of evidence the
  breaker itself refuses to trip on.

## Considered Options

- Auto-resume once the posterior lower bound drops back under a threshold
- Auto-resume after a fixed cooldown period (e.g. 48 hours)
- Never auto-resume; require an explicit human action

## Decision Outcome

Chosen option: "Never auto-resume; require an explicit human action."

`engine.breaker.BreakerStateStore` has exactly one method that can move a
mailbox from `PAUSED` back to `ACTIVE`: `resume_after_human_review`. It is
never called from `evaluate()` or its internal `_act()` helper --
`tests/test_breaker.py::test_evaluate_never_calls_resume_after_human_review`
enforces this by inspecting the source of both functions directly, not just
by convention.

### Consequences

- Good, because a mailbox never resumes sending without someone having
  looked at why it was paused -- the failure mode of "paused forever because
  nobody looked" is visible and annoying (a human has to notice and act);
  the failure mode of "resumed and re-triggered the same problem" is
  invisible until it's already happened again.
- Good, because it sidesteps the low-volume-evidence-after-pause problem
  entirely: there is no threshold to tune for "how sure do we need to be
  that it's fixed," because the system never has to be sure -- a human is.
- Bad, because a paused mailbox stays paused, and revenue-generating sending
  stays off, until someone does something about it. This is a real,
  deliberate cost, not a free lunch.
- Bad, because this pushes an operational burden onto whoever operates this
  tool: something (a person, a runbook, eventually a CLI command) has to
  actually go call `resume_after_human_review` -- there is currently no
  built-in reminder or escalation for a stuck-paused mailbox. That's a real
  gap, noted as a known limitation below rather than solved here.

### Confirmation

`tests/test_breaker.py::test_evaluate_never_calls_resume_after_human_review`
asserts by source inspection that neither `evaluate()` nor `_act()`
reference `resume_after_human_review`. Any future change that adds an
automatic resume path would need to either modify that test (a visible,
reviewable diff) or route around it in a way that would stand out in code
review.

## Assumption this relies on

That a human will actually notice a paused mailbox and act on it in a
reasonable timeframe. Nothing in this project currently pages anyone or
surfaces "still paused after N days" as its own alert -- the decision log
(`audit/log.py`) records the pause, but reading it is still someone's job.

## Known limitation

This decision creates an operational gap this project does not yet fill: a
paused mailbox with no human attention stays paused indefinitely, silently,
forever. A future version should probably surface "mailboxes paused for
longer than N days" as its own notification -- but that is itself a new
signal with its own honesty requirements (BUILD-PLAN.md's whole thesis is
about not overclaiming), not a trivial addition, so it's deliberately out of
scope here rather than bolted on half-thought-through.

## Pros and Cons of the Options

### Auto-resume once the posterior lower bound drops back under a threshold

- Good, because it's fully automatic -- no human bottleneck
- Bad, because "a mailbox with near-zero sends looks fine on paper" is
  exactly the failure mode this project is built to distrust when deciding
  to PAUSE; using the same kind of low-confidence evidence to decide to
  RESUME is inconsistent with that

### Auto-resume after a fixed cooldown period

- Good, because it's simple and predictable
- Bad, because a fixed cooldown has no relationship to whether anything was
  actually fixed -- it's a timer, not a check

### Never auto-resume; require an explicit human action (chosen)

- Good, because it never resumes sending based on evidence too thin to
  trust, by construction
- Bad, because it requires a human in the loop and has no built-in
  mechanism yet to make sure that human shows up (see "Known limitation")

## Addendum (2026-08-30): THROTTLE was not idempotent, and could reach a de-facto pause without ever going through this gate

An external audit (ENG-5a) found that while PAUSE was guarded against
repeat application (the whole point of this ADR), THROTTLE was not. Every
evaluation tick that reached the THROTTLE rung halved the mailbox's daily
limit again -- six identical ticks took a limit of 50 down to 1
(`50 -> 25 -> 12 -> 6 -> 3 -> 1`) without the mailbox ever entering
`PAUSED`, and therefore without ever passing through the human-review gate
this ADR exists to enforce. `_MIN_THROTTLED_DAILY_LIMIT`'s own comment
already named the failure mode ("a mailbox throttled all the way to 0/day
is a pause wearing a different hat") and the code walked into it anyway,
one halving at a time.

**Fix:** `MailboxBreakerStatus` gained a `THROTTLED` member. `_act`'s
THROTTLE branch is now idempotent the same way PAUSE's is: a mailbox
already `THROTTLED` when the verdict is still THROTTLE is a no-op, keyed on
the *verdict*, not the numeric limit. Separately, `evaluate` now escalates
a THROTTLE verdict to PAUSE *before* acting on it whenever halving the
current daily limit would fall below the floor
(`_MIN_THROTTLED_DAILY_LIMIT`) -- so a mailbox that would otherwise be
floor-clamped forever is routed through PAUSE, and therefore through
`resume_after_human_review`, instead.

### Confirmation

`tests/test_breaker.py::test_repeated_throttle_verdict_does_not_re_halve_the_daily_limit`
asserts six identical THROTTLE evaluations produce exactly one `throttle()`
call. `test_throttle_that_would_drop_below_the_floor_escalates_to_pause`
asserts a limit of 1 produces `Verdict.PAUSE` and a real `pause()` call,
not a floor-clamped throttle -- this replaces an old assertion
(`driver.throttle_calls == [("a@example.com", 1)]`) that encoded the exact
bug this fixes.

## Addendum (2026-08-30): the never-auto-resume guarantee didn't survive a process restart

The same audit (ENG-5b) found that `BreakerStateStore` was in-memory only,
with `status_of` defaulting any mailbox it had never seen to `ACTIVE`.
Restarting the process therefore reset every previously-`PAUSED` mailbox
back to `ACTIVE` with no human ever touching it -- this ADR's guarantee was
enforced by process uptime alone, the weakest possible enforcement, and
invisible in every test because no test had ever restarted the process.

**Fix:** `BreakerStateStore.from_log(path)` rebuilds pause/throttle status
by replaying `audit.log`'s append-only decision log in order. A log file
that doesn't exist yet is genuinely "no history" (a new deployment) and
correctly produces an empty, all-`ACTIVE` store. A log file that exists but
can't be read or parsed is a *different* situation -- silently falling back
to that same empty store would be indistinguishable from "no history" and
could un-pause a mailbox the log actually says is `PAUSED`, so this now
raises `BreakerStateStoreLoadError` instead of returning a store at all.

**Known limitation, carried forward deliberately:** `resume_after_human_review`
doesn't itself produce a decision record (it isn't a `BreakerEvaluation`),
so a mailbox resumed by a human and never re-evaluated before a restart
will rebuild as `PAUSED` rather than `ACTIVE`. This is a real gap, but an
intentionally fail-safe one: on ambiguity, `from_log` errs toward `PAUSED`,
never toward `ACTIVE`, which is the same asymmetry this ADR already argues
for everywhere else. A future version should log resume events too (once
the CLI's `resume` command exists to originate them) so this gap closes
rather than being merely safe.

### Confirmation

`tests/test_breaker.py::test_state_store_rebuilds_paused_status_from_the_decision_log`
is the restart reproduction: pause a mailbox, rebuild a fresh store from
the same log, assert it's still `PAUSED`. Companion tests cover THROTTLED
rebuild, a failed pause attempt correctly rebuilding as `ACTIVE`, a paused
mailbox staying paused through later healthy-looking log entries, and the
no-history-vs-unreadable-history distinction.

## Addendum (2026-08-30): the MCP server is read-only, on purpose

`mcp_server.py` (BUILD-PLAN.md §4 item #27) wraps this project's read
surface -- breaker status, configured thresholds, recent decision-log
entries -- as MCP tools. It deliberately does NOT expose `resume`,
`pause`, or `throttle`. Wiring `resume_after_human_review` up as an
LLM-callable MCP tool would hand the resume decision to whatever is on
the other end of the MCP connection, which is exactly the automatic-
resume path this ADR exists to rule out -- an MCP client asking an LLM
"should I resume this mailbox?" and getting a "yes" is not meaningfully
different from the auto-resume-on-a-quiet-posterior option this ADR
already rejected, just with an LLM's judgment substituted for a
threshold. If a write surface is ever wanted here, that is a new decision
requiring its own ADR, not a quiet extension of `mcp_server.py`.
`tests/test_mcp_server.py::test_build_server_never_registers_a_pause_or_resume_tool`
enforces this by inspecting the server's registered tool names directly.

## Addendum (2026-08-31): the never-auto-resume guarantee still didn't survive a restart, in the other direction

A second external audit (CLOSE-4) found that the ENG-5b fix above only
closed half the gap it named as a known limitation: `resume_after_human_review`
still wrote nothing to the decision log. End to end, through the CLI's own
functions, a mailbox that was paused, resumed, and then restarted came back
`PAUSED` -- the resume was silently lost, and `resume` was the *only*
documented way out of `PAUSED` (ADR 0003's whole point), so in practice there
was no way back short of hand-editing the JSONL.

Worse, the same audit found `from_log` never inspected `record.dry_run`: a
dry-run evaluation that reached `PAUSE` still wrote a record with
`verdict=PAUSE, action_outcome=PERFORMED` (the engine's own action result,
which AGENTS.md requires stay identical between dry-run and live -- see
`providers.base.ActionOutcome.DRY_RUN`'s docstring), and `from_log` read that
back as a real, persisted `PAUSED` state. A dry-run deployment -- one
explicitly configured to never touch a real mailbox -- would therefore
accumulate durable `PAUSED` state anyway, with no supported way back, which
is a direct violation of AGENTS.md's dry-run non-negotiable.

**Fix, three parts:**

1. `audit.log.ResumeRecord` is a new record type, written by `cli.cmd_resume`
   to the same JSONL log via `append_resume_record`, carrying who resumed and
   when. `audit.log.read_events` reads decision and resume records back
   together, in file order; `BreakerStateStore.from_log` replays both,
   applying a `ResumeRecord` as an unconditional move to `ACTIVE` at the
   point in the sequence it occurred. `resumed_by` is a required field on
   both `cmd_resume` and `ResumeRecord` -- never defaulted to something
   meaningless -- because this ADR's whole argument is that a specific human
   is accountable for this decision.
2. `DecisionRecord.from_evaluation` now records a dry-run action's outcome as
   `ActionOutcome.DRY_RUN`, distinct from `PERFORMED`, in the LOG only --
   `BreakerEvaluation.action.outcome` itself, and `engine.breaker._act`'s
   idempotency logic, are untouched, preserving AGENTS.md's "dry-run must
   produce decisions identical to the live path" at the engine level. Only
   the persisted record, whose job is to tell a human or `replay()` what
   actually happened in the world, needs to say "would have paused," not
   "paused."
3. `BreakerStateStore.from_log` now also skips any decision record whose
   `dry_run` is `True` when deriving status, independent of (1) -- belt and
   suspenders, since a future bug in (2) should not be able to reintroduce
   dry-run-derived persisted state on its own.

Separately, a zero-byte decision log (distinguishable from a genuinely
missing one, but easy to produce by a crash mid-write, a `touch`, or a
truncated filesystem) previously fell through to `read_records` returning an
empty list, which `from_log` read as "no history yet" -- exactly as fail-open
as the ENG-5b bug this ADR already exists to prevent, just via a different
code path. `from_log` now raises `BreakerStateStoreLoadError` for an
existing-but-empty log, the same as any other unreadable log.

### Confirmation

`tests/test_breaker.py::test_state_store_rebuilds_active_status_from_a_resume_record_after_pause`
and `tests/test_cli.py::test_main_resume_survives_a_restart_end_to_end` are
the restart reproductions for (1). `tests/test_breaker.py::
test_state_store_from_log_skips_a_dry_run_pause_record` and
`tests/test_cli.py::test_main_check_dry_run_pause_does_not_survive_a_restart`
cover (2)/(3). `tests/test_breaker.py::test_state_store_from_log_fails_closed_on_an_empty_log`
covers the zero-byte case.

## Addendum (2026-08-31): the throttle rung latched, reopened, and never fired in production (CLOSE-3)

A third finding from the same follow-up audit (CLOSE-3), specifically about
the THROTTLE rung rather than PAUSE:

1. **THROTTLE never reached the provider at all** on the real `check`/`run`
   path, because `loops.fast.evaluate_all_mailboxes` never passed
   `current_daily_limit`. Fixed as part of the CLOSE-1 wiring commit (see
   ADR 0002's addendum for that).
2. **Once THROTTLED, THROTTLE was a permanent no-op.** Nothing ever cleared
   `THROTTLED` back to `ACTIVE` on recovery, and the ENG-5a idempotency fix
   above was keyed purely on status, not on whether the mailbox's daily
   limit had actually changed since. Concretely: `THROTTLE -> OK -> THROTTLE`
   never reached the provider a second time, and a human manually restoring
   a throttled mailbox's daily limit -- without an intervening `OK` verdict
   -- was invisible to the idempotency check entirely.
3. **A failed PAUSE reopened the THROTTLE cascade.** `_act` marked a FAILED
   pause attempt `ACTIVE`, the same status as a mailbox that had never been
   touched. A subsequent THROTTLE verdict against that "pristine-looking"
   mailbox re-halved an already-throttled limit (25 -> 12), continuing the
   exact cascade ENG-5a's idempotency fix was supposed to prevent.
4. **Floor escalation had an off-by-one.** The guard was
   `current_daily_limit // 2 < _MIN_THROTTLED_DAILY_LIMIT` (`< 1`). At a
   limit of 2 or 3, `// 2` is exactly `1` -- the floor -- so the guard never
   fired, and the mailbox was silently clamped to 1/day (a de-facto pause)
   without ever passing through the human-review gate this ADR exists to
   enforce.

**Fix, matching the four numbered points above:**

1. See ADR 0002's addendum on wiring.
2. `engine.breaker.evaluate` now clears `THROTTLED` back to `ACTIVE` whenever
   it computes a plain `OK` verdict (never on `WARN`). Idempotency is now
   also keyed on the daily limit, not just the status:
   `BreakerStateStore` tracks `throttled_at_limit(mailbox)`, the
   `current_daily_limit` input in force the last time this mailbox was
   actually throttled. A THROTTLE verdict is a no-op only when the mailbox's
   *current* limit hasn't grown past that recorded value; if it has (a
   human restored it, with no intervening `OK`), that's treated as a fresh
   throttle event and acted on again.
3. `MailboxBreakerStatus` gained `PAUSE_FAILED`, distinct from `ACTIVE`. A
   FAILED pause attempt now marks `PAUSE_FAILED`, which -- unlike
   `mark_active` -- does NOT clear `throttled_at_limit`, so a later THROTTLE
   still compares against what was actually applied and stays idempotent.
4. The floor-escalation guard is now `current_daily_limit // 2 <=
   _MIN_THROTTLED_DAILY_LIMIT` -- escalates when the RESULT would be at or
   below the floor, not only strictly below it.

**Known limitation, carried forward deliberately:** a `BreakerStateStore`
rebuilt via `from_log` does not restore `throttled_at_limit` -- the decision
log doesn't currently persist the `current_daily_limit` input a throttle was
computed against. The first THROTTLE evaluation after a restart will
therefore act once more even if nothing has actually changed, recomputing a
fresh (still correct) limit rather than a stale one. This is a real gap, but
not the ENG-5a-style unbounded cascade this ADR guards against -- it's at
most one extra, correctly-computed call per restart.

### Confirmation

`tests/test_breaker.py::test_throttle_then_ok_then_throttle_re_throttles`,
`test_throttle_re_throttles_when_the_current_limit_has_grown_past_what_was_applied`,
`test_failed_pause_does_not_let_a_later_throttle_re_halve`,
`test_daily_limit_of_two_escalates_to_pause`, and
`test_daily_limit_of_three_escalates_to_pause` cover points 2-4 above, and
`test_repeated_throttle_verdict_does_not_re_halve_the_daily_limit` (unchanged)
confirms the six-identical-ticks case still holds.

## Addendum (2026-09-01): a FAILED pause attempt was a permanent latch, and PAUSE_FAILED now recovers like THROTTLED does

An external audit (CLOSE9-1) found that `MailboxBreakerStatus.PAUSE_FAILED`
had a latch bug this ADR's own CLOSE-3b addendum already fixed for
`THROTTLED`, and never carried over: `mark_pause_failed`'s
`throttled_at_limit` memo is correct advice for the VERY NEXT evaluation
("don't re-halve an already-throttled limit just because the pause on top
of it failed" -- CLOSE-3c), but nothing ever moved a mailbox OFF
`PAUSE_FAILED` at all. `cmd_resume` refused it (only `PAUSED` and
`THROTTLED` were accepted). CLOSE-3b's single-OK recovery checked only
`THROTTLED`. So a mailbox whose pause attempt failed once stayed
`PAUSE_FAILED` -- and its stale `throttled_at_limit` memo stayed live --
through any number of subsequent healthy evaluations, forever. Reproduced:
30 consecutive healthy evaluations after one `PAUSE_FAILED`, then a fresh
breach at 30x Gmail's ceiling with the operator's real daily limit now
100 (down from the stale 400 on file) -- `_act` read this as "mailbox
already throttled at this limit; no action taken (idempotent)" and made
**zero** real provider calls, permanently, for a mailbox that had been
healthy for a month.

### Why this is a DIFFERENT question from "never auto-resume PAUSED"

This ADR's core decision -- never auto-resume a `PAUSED` mailbox -- rests
on a real pause having landed at the provider: revenue-risking sending has
actually STOPPED, and restarting it without a human looking first is the
failure mode this ADR exists to prevent (see "Decision Drivers" above).
`PAUSE_FAILED` is the opposite case: the provider explicitly said no, so
the mailbox never stopped sending at all. It is closer in shape to
`THROTTLED` (a real action was attempted, the mailbox is still live
either way, and CLOSE-3b already lets a single healthy evaluation clear
`THROTTLED` automatically) than to `PAUSED` (a real action landed,
sending genuinely stopped, and only a human may restart it). Treating
`PAUSE_FAILED` as automatically-recoverable is therefore consistent with
this ADR's existing THROTTLED precedent, not an exception carved into it.

### Decision Outcome

`PAUSE_FAILED` now recovers exactly like `THROTTLED` does, on both paths:

1. `cmd_resume` now accepts `PAUSE_FAILED` alongside `PAUSED` and
   `THROTTLED` -- a human can unstick it immediately, the same way
   CLOSE3-3 let a human clear a stuck `THROTTLED` mailbox. The refusal
   message for every other status now names what CAN be resumed, instead
   of only naming what the mailbox isn't.
2. `evaluate()`'s and `from_log`'s single-OK recovery check (CLOSE-3b) now
   fires for `PAUSE_FAILED` the same as it already does for `THROTTLED`: a
   `Verdict.OK` evaluation (real evidence, not `INSUFFICIENT_DATA` --
   CLOSE7-1's own guard already ensures that) moves the mailbox to
   `ACTIVE` and clears `throttled_at_limit`.

Both clear `throttled_at_limit`, which is the part that actually matters:
CLOSE-3c's original property (a failed pause must not make the
IMMEDIATELY NEXT throttle re-halve an already-throttled limit) still
holds, because nothing about this changes what happens on the evaluation
right after the failed pause -- only what happens after SUSTAINED healthy
evidence, which is a different question this ADR's own THROTTLED
precedent already answered the same way.

### Confirmation

`tests/test_breaker.py`'s `test_pause_failed_recovers_after_thirty_healthy_days`
is the reproduction, run to completion rather than stopped at the bug: 30
healthy evaluations after a FAILED pause, then a fresh breach, asserting a
REAL `throttle()` call happens. `test_resume_accepts_a_pause_failed_mailbox`
covers the human path. `test_failed_pause_does_not_let_a_later_throttle_re_halve`
(CLOSE-3c's own test, unchanged) confirms the immediately-next-evaluation
property this addendum does not touch. A live-versus-should property test
(not a live-versus-replay one -- see `tests/test_breaker.py`'s blind-spot
list, item 8) asserts that no bounded run of healthy evaluations can leave
a mailbox in a state from which no provider action is ever possible again.

## Addendum (2026-09-01): "sustained recovery" was renamed to "single-OK recovery" -- the behavior did not change

An external audit (CLOSE10-3) measured what CLOSE-3b's recovery path (and
its CLOSE9-1 extension to `PAUSE_FAILED`, above) actually requires, rather
than trusting the word this project's own docstrings and this ADR had used
for it since CLOSE-3b shipped:

```
ONE OK eval with sends=1, complaints=0 -> verdict=OK data_state=OK status=ACTIVE memo=None
   then THROTTLE @25 -> new calls=[('throttle', 'ops@acme.example', 12)]
```

One evaluation, with a SINGLE send, clears `THROTTLED` (or, since CLOSE9-1,
`PAUSE_FAILED`) back to `ACTIVE` and lets the very next THROTTLE re-halve.
Every docstring and comment describing this called it "sustained
recovery" or "a sustained OK verdict" -- language this project's own
`docs/statistics.md`, `engine/state.py`'s `DataState`, and AGENTS.md's
"never claim a statistical property the data cannot support" all argue
against using loosely. "Sustained" implies a run of evidence over time;
what actually fires is `n=1`.

This is NOT a CLOSE9 regression, and not a bug on its own: `THROTTLED`
behaved identically at `f9077ff`, well before CLOSE9-1 touched this code
at all, and CLOSE7-1's own `INSUFFICIENT_DATA` guard (an evaluation with
`sends == 0` is `Verdict.OK` but `DataState.INSUFFICIENT_DATA`, and never
reaches this recovery check at all) is precisely what stops SILENCE from
triggering it -- the guard that exists is doing real work; it just isn't
the word "sustained" implies. The genuine choice this finding forces is
between two honest options, not a defect to patch:

- **Rename it.** Call it what it measurably is -- single-OK recovery, not
  sustained recovery -- everywhere the word appears, with no change to
  when it fires.
- **Make it true.** Require `k` consecutive `OK` evaluations, or a minimum
  send volume, before clearing. A real behavior change, needing its own
  threshold (arbitrarily picking a `k` or a volume with no data behind it
  would be a NEW overclaim, of a different kind, dressed up as a fix) and
  its own tests for the new boundary.

### Decision Outcome

Chosen: **rename it.** `n=1` recovery is not wrong -- CLOSE-3b's whole
point was that `THROTTLE`, unlike `PAUSE`, has an automatic way back once
there's real evidence again, and one clean day IS real evidence (`sends >=
1`, `DataState.OK` -- the `INSUFFICIENT_DATA` guard already rules out
silence masquerading as it). What was wrong was calling one data point
"sustained." Manufacturing a `k`-evaluation or minimum-volume threshold
under this finding's own time pressure, with no statistical grounding for
whatever number got picked, would trade one overclaim (the word) for
another (a fabricated-precision threshold) -- exactly the failure mode
`docs/statistics.md` and AGENTS.md's non-negotiable #3 exist to rule out.
If a real, data-grounded case for requiring more evidence before recovery
emerges later, that is a genuine behavior change and deserves its own ADR
addendum, argued on its own evidence -- not a number chosen to make this
finding go away.

Every docstring, comment, and test description using "sustained recovery"
/ "sustained OK verdict" / "sustained healthy evaluations" for this
specific mechanism was renamed to "single-OK recovery" (`engine/breaker.py`,
`cli.py`, `docs/decisions/0008`, `tests/test_breaker.py`). Deliberately
NOT renamed: `engine/changepoint.py`'s and `loops/fast.py`'s own uses of
"sustained shift" / "sustained elevated rate" describing CUSUM's actual
detection target (a genuinely multi-period accumulation, a different
mechanism entirely) -- those uses were already accurate and renaming them
would have been the same mistake in the other direction.

### Confirmation

No test changed behavior; this is a pure rename. The existing recovery
tests (`test_throttle_then_ok_then_throttle_re_throttles`,
`test_state_store_from_log_recovers_a_throttled_mailbox_after_five_healthy_restarts`,
CLOSE9-1's `test_pause_failed_recovers_after_thirty_healthy_days`) still
pass unmodified in substance -- only their docstrings' wording changed.

## More Information

See `engine/breaker.py`'s `BreakerStateStore` docstring and
`docs/statistics.md` for why low-volume evidence is untrustworthy in either
direction, not just the direction this project already argues loudly about.
