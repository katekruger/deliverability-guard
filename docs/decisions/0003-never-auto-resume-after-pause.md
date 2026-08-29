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

## More Information

See `engine/breaker.py`'s `BreakerStateStore` docstring and
`docs/statistics.md` for why low-volume evidence is untrustworthy in either
direction, not just the direction this project already argues loudly about.
