---
status: "accepted"
date: "2026-08-30"
deciders: "Kate Kruger"
---

# The always-on daemon polls instead of receiving webhooks, and the slow loop tunes from its own recent evidence, not live Postmaster data

## Context and Problem Statement

BUILD-PLAN.md §5 describes a two-loop controller: a fast loop reacting to
webhooks and other leading indicators in seconds-to-minutes, and a slow
loop that tunes the fast loop's thresholds daily from Postmaster data and
compliance verdicts. At the time this ADR was written, neither loop ran
continuously at all -- `cli.py` was empty and the only way to evaluate a
mailbox was a manual one-shot `check` invocation (see ADR history and
`CHANGELOG.md`'s ENG-6 entry). Building the "real, always-on system" raised
two design questions BUILD-PLAN.md left open:

1. How does the fast loop actually receive events, if nothing in this
   codebase accepts an inbound webhook?
2. Where does the slow loop's "recent evidence" come from, if wiring live
   Google Postmaster access (OAuth, a token refresh flow, per-tenant
   credentials) is a substantial separate integration in its own right?

## Decision Drivers

- Ship an always-on daemon a user can actually run today, with zero
  additional infrastructure (no HTTP server, no OAuth setup) beyond what
  `check` already needs.
- Don't let the one-shot `check` command and the continuous daemon drift
  into two different evaluation implementations that could disagree.
- Don't overclaim: if the daemon's "fast loop" isn't truly a webhook
  receiver, and its "slow loop" isn't truly reading Postmaster, the
  documentation and code must say so plainly rather than implying BUILD-
  PLAN.md §5's architecture is fully realized.

## Considered Options

- Build a webhook-receiving HTTP server (fast loop) and a full Postmaster
  OAuth client wired into the slow loop, matching BUILD-PLAN.md §5 exactly.
- A polling fast loop (reusing `driver.read_mailbox_stats`) and a slow loop
  that tunes thresholds from its own rolling window of recent posterior
  lower bounds, with Postmaster/compliance signals as an optional injected
  callback rather than a hard dependency.
- Ship only `check` (one-shot) and defer the daemon entirely.

## Decision Outcome

Chosen option: "a polling fast loop and a self-sourced slow loop, with
Postmaster as an optional injection point."

**Fast loop is polling, not webhook-receiving.** `loops/controller.py`'s
fast tick calls `driver.read_mailbox_stats` on a configurable interval
(`fast_interval_seconds`, default 300) and evaluates every mailbox through
the exact same `loops.fast.evaluate_all_mailboxes` that `cli.cmd_check`
uses -- so the one-shot and continuous forms cannot drift apart. This is a
strictly weaker substitute for "seconds-to-minutes reaction to leading
indicators": a real bounce takes up to one polling interval to be noticed,
not the immediate reaction a pushed webhook would give. It was chosen
because building a correct webhook receiver (an HTTP server, per-provider
signature verification, replay protection beyond the existing
`WebhookLedger`) is real, separate infrastructure work with its own
security surface, not a small addition to this module.

**Slow loop tunes from its own rolling window, not live Postmaster.** The
daemon collects each fast tick's posterior lower bounds into a bounded
window (`DEFAULT_LOWER_BOUND_WINDOW = 500`) and, once
`slow_interval_seconds` (default 86400) has elapsed, hands that window to
the existing `loops.slow.tune_thresholds` -- unmodified, and still
completely decoupled from `providers`/`signals` per its own docstring's
constraint. A caller with a live Postmaster/compliance signal can wire it
in via the `compliance_degraded: Callable[[], bool]` parameter; nothing
about this design blocks that from being layered on later.

### Consequences

- Good, because a user can run `deliverability-guard run` today with
  nothing beyond what `check` already requires -- no webhook endpoint to
  expose, no OAuth flow to complete.
- Good, because `check` and `run`'s fast tick share one evaluation path
  (`loops.fast.evaluate_all_mailboxes`), so they cannot silently diverge.
- Good, because the slow loop's contract (`loops.slow.tune_thresholds`) is
  untouched -- Postmaster/compliance wiring remains a pure addition, not a
  rework, whenever it's built.
- Bad, because polling is a real, honest downgrade from "seconds-to-minutes
  reaction to leading indicators": detection latency is bounded below by
  `fast_interval_seconds`, not by how fast a webhook arrives.
- Bad, because the slow loop's "recent evidence" is only ever this
  process's own fast-loop history. It has no memory across restarts (the
  window is in-memory, not persisted) and no Postmaster-derived signal at
  all unless a caller supplies `compliance_degraded` themselves -- so a
  freshly restarted daemon starts the slow loop from an empty window every
  time.

### Confirmation

`tests/test_controller.py` exercises the full tick loop with an injected
clock (`now`/`sleep` are always callables, never the real clock or
`time.sleep`) -- fast ticks calling `evaluate_all_mailboxes`, the shared
`BreakerStateStore` making repeat PAUSE/THROTTLE idempotent across ticks
exactly as `check` is, the slow loop tightening `ThresholdStore` only once
`slow_interval` has elapsed, and `compliance_degraded` reaching
`tune_thresholds` even with an empty evidence window.

## Assumption this relies on

That a bounded polling interval (default 5 minutes) is an acceptable
approximation of "leading indicator" reaction time for this project's
target audience (cold-outbound senders, not high-volume transactional
mail) -- BUILD-PLAN.md's own worked examples are at 50-5,000 sends/day,
where a few minutes of polling lag is immaterial next to the 24h-3-day
complaint-reporting lag this entire project already treats as a design
constraint.

## Known limitation

This does not implement BUILD-PLAN.md §5's architecture as originally
specified: there is no webhook receiver, and the slow loop has no live
Postmaster/`getComplianceStatus` signal built in. Both are real,
independently scoped pieces of future work -- a webhook HTTP server with
per-provider signature verification, and Postmaster OAuth wiring that
supplies `compliance_degraded` and/or hierarchically-pooled posteriors
(`engine.posterior.pooled_posterior`) as the slow loop's evidence instead
of (or alongside) the fast loop's own rolling window. Anyone building
either should not need to change `loops/controller.py`'s core loop, only
supply a richer signal into the parameters that already exist for it.

## Pros and Cons of the Options

### Full webhook receiver + Postmaster OAuth, matching BUILD-PLAN.md §5 exactly

- Good, because it's the architecture as originally specified, with no gap
  to document
- Bad, because it's a multi-week scope on its own (HTTP server, signature
  verification per provider, OAuth token lifecycle) that would have
  blocked shipping any always-on daemon at all

### Polling fast loop + self-sourced slow loop (chosen)

- Good, because it ships today, with zero additional infrastructure
- Good, because it doesn't foreclose the full version -- both gaps have a
  clean extension point already
- Bad, because it's a real, documented downgrade from the original
  architecture, not the thing itself

### Ship only `check`, defer the daemon entirely

- Good, because it adds no new surface area or risk
- Bad, because it leaves ENG-6's "no running system" finding half-addressed
  -- a user still can't run this continuously without hand-rolling their
  own cron-plus-scripting equivalent of what this ADR's chosen option
  provides directly

## More Information

See `src/deliverability_guard/loops/controller.py` for the implementation,
`tests/test_controller.py` for the behavioral contract, and `CHANGELOG.md`'s
entry for this change.
