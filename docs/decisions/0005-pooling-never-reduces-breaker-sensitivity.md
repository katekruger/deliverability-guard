---
status: "accepted"
date: "2026-08-31"
deciders: "Kate Kruger"
---

# Hierarchical pooling must never make the breaker less sensitive than evaluating a mailbox alone

## Context and Problem Statement

ADR 0002's ESS cap (2026-08-30 addendum) fixed hierarchical pooling from
complete pooling (a large peer group could swamp any individual mailbox's
own evidence) to genuinely partial pooling: a mailbox with enough of its own
evidence is guaranteed a bounded floor of weight in its own posterior,
however large its peer group grows.

A follow-up external audit (CLOSE-2) found the cap alone was not enough. At
**low-to-moderate** own volume -- specifically between roughly 91 and 389
own sends in the audit's reproduction -- pooling with a large, healthy peer
group could still make the breaker's decision **strictly less sensitive**
than evaluating the mailbox's own evidence alone would have been:

| own_sends | pooled lower95 | pooled breach | flat lower95 | flat breach |
|---|---|---|---|---|
| 200 | 0.1692% | **False** | 0.8307% | **True** |
| 100 | 0.1035% | **False** | 0.3823% | **True** |

A mailbox sending at 5% (16x Gmail's 0.3% ceiling) breached when evaluated
on its own evidence alone, but did NOT breach once pooled with a domain of
healthy peers, in exactly the cold-outbound volume band (BUILD-PLAN.md §1)
this project exists to serve. The ESS cap bounds how much weight the GROUP
can carry, but it does not bound how much the group's evidence can pull the
POSTERIOR itself toward "healthy" for a mailbox whose own evidence, alone,
would already have tripped the breaker.

## Decision Drivers

- Pooling exists to rescue a mailbox with too little of its OWN evidence to
  say anything, by borrowing strength from its peers (ADR 0002). It must
  never be usable, even accidentally, to make a mailbox with ENOUGH of its
  own bad evidence look safer than evaluating it alone would.
- The fix must preserve the legitimate case pooling exists for: a mailbox
  with almost no data of its own (n=1) sitting on a large healthy peer group
  must still correctly read as `INSUFFICIENT_DATA`-adjacent / not breaching,
  not be forced to breach just because pooling now interacts with the flat
  evaluation somehow.
- Whatever the fix is, it must be a guarantee simple enough for an operator
  to hold in their head and trust without re-deriving the math -- this
  project's whole posture (BUILD-PLAN.md §2) is about honest, legible
  statistical claims.

## Considered Options

- Take the worse of the pooled and flat posteriors' lower bounds -- breach
  if either does.
- Scale `max_ess` (the pooling cap) with the mailbox's own volume, e.g. cap
  the group's ESS at some multiple of the mailbox's own sends.
- Floor the mailbox's own weight in its posterior at some fixed fraction,
  regardless of group size.

## Decision Outcome

Chosen option: **"Take the worse of the pooled and flat posteriors' lower
bounds."** `engine.breaker.evaluate` now computes the flat (non-pooled)
posterior's lower bound whenever `peer_group` is given, in addition to the
pooled one, and uses `max(pooled_lower_bound, flat_lower_bound)` to decide
the verdict. This is the only option whose guarantee a user can state in one
sentence and trust without checking the math: **pooling can only ever ADD
sensitivity to the breaker, never remove it.** The flat evaluation -- what
the breaker would have decided with no pooling at all -- is always still
checked; pooling is purely additive on top of it.

The other two options were rejected because they trade the SIZE of the
problem for the SIZE of a tuning parameter, rather than closing it: scaling
`max_ess` with own volume still leaves *some* volume band where the group
can outweigh a mailbox's own bad evidence (just a different band, chosen by
a coefficient nobody can point to a principled value for), and a fixed own-
weight floor has the same shape of problem -- there is no floor low enough
to preserve pooling's benefit for a genuinely low-volume mailbox that is
also guaranteed high enough to prevent this exact masking at every own-
volume level. "Take the worse of the two" sidesteps needing to find that
number at all.

### Consequences

- Good, because the guarantee is exactly as strong as it sounds: for any
  `own_sends`/`own_complaints`/`peer_group`, `evaluate()`'s effective lower
  bound is never less than what the same mailbox's own evidence alone would
  produce. Verified as a property test sweeping `own_sends` over
  `[1, 10, 50, 100, 200, 500, 1000, 5000]` at a 5% true rate against 999
  healthy peers (`tests/test_breaker.py::
  test_pooled_breach_at_the_verdict_level_is_true_wherever_flat_breach_is_true`).
- Good, because the legitimate low-volume case is untouched: at `n=1`, the
  flat lower bound is itself tiny (not enough evidence to say anything), so
  `max(pooled, flat)` is dominated by whichever is small, and the mailbox
  correctly does not breach regardless of peer group health or size
  (`test_n1_at_100_percent_against_999_healthy_peers_still_does_not_breach`).
- Good, because this required no new tuning parameter and no change to
  `pooled_prior`/`pooled_posterior` themselves -- the fix is entirely in how
  `evaluate()` USES the pooled result, which keeps ADR 0002's own pooling
  math and its ESS-cap addendum unchanged and independently correct.
- Bad, because a mailbox can no longer be read as "safer" purely by virtue
  of a healthy peer group when its own evidence alone already says
  otherwise -- this is the intended behavior, not a limitation, but it does
  mean pooling's UPSIDE (making a marginal mailbox look more confidently
  healthy) only ever applies when the mailbox's own evidence doesn't already
  say the opposite.
- Bad, because `BreakerEvaluation.lower_bound` and `.posterior` can now
  disagree in a way that looks surprising on first read: `.posterior` is
  always the raw pooled `BetaDistribution` (for audit/inspection -- "what
  did the pooling actually compute"), while `.lower_bound` is the EFFECTIVE
  value that drove the verdict (which may come from the flat evaluation
  instead). This is documented at the call site and in
  `engine.breaker.evaluate`'s inline comment, but a future reader
  constructing `.posterior.lower_bound()` themselves and expecting it to
  match `.lower_bound` will be surprised if `peer_group` was given.

### Confirmation

`tests/test_breaker.py::test_pooled_breach_at_the_verdict_level_is_true_wherever_flat_breach_is_true`
(parametrized over the own-volume sweep above) and
`test_n1_at_100_percent_against_999_healthy_peers_still_does_not_breach`
encode the two required properties directly.
`test_evaluate_peer_group_lower_bound_is_never_below_the_flat_one` pins the
`max()` behavior at a single worked example matching this ADR's own
reproduction table.

## Assumption this relies on

That a mailbox's OWN flat posterior (no pooling at all) is itself a
trustworthy, if sometimes over-conservative, signal -- ADR 0001/0002 already
established this is true (that's the entire reason `update()`/`lower_bound`
exist). This decision just insists that signal is never discarded once
computed, only ever supplemented.

## Known limitation

`BreakerEvaluation.lower_bound` no longer always equals
`BreakerEvaluation.posterior.lower_bound(confidence)` when `peer_group` was
given -- see "Consequences" above. `audit.log.DecisionRecord` doesn't
currently persist which of the two (pooled or flat) was decisive for a given
record, only the resulting `lower_bound` value itself; `audit.log.replay()`
also has no access to the original `peer_group`, so it cannot reproduce a
pooled verdict from the log alone regardless of this decision (a pre-
existing limitation, not introduced by it -- `replay()` has only ever
recomputed the flat posterior). Making pooled evaluations fully replayable
from the log is future work, not solved here.

## Pros and Cons of the Options

### Take the worse of the pooled and flat posteriors' lower bounds (chosen)

- Good, because the resulting guarantee is a single sentence a user can
  hold in their head and trust
- Bad, because it computes the flat posterior on every pooled evaluation
  (cheap -- a single closed-form `update()` call -- but not free)

### Scale `max_ess` with the mailbox's own volume

- Good, because it's a single-parameter change to `pooled_prior`, no new
  comparison logic in `evaluate()`
- Bad, because the right multiplier is not principled, and whatever it is,
  it still leaves SOME own-volume band where masking is possible again,
  just moved rather than closed

### Floor the mailbox's own posterior weight at a fixed fraction

- Good, because it's conceptually simple: "your own evidence always counts
  for at least X% of your posterior"
- Bad, because the fraction that's high enough to prevent masking at every
  own-volume level is also high enough to significantly weaken pooling's
  benefit for genuinely low-volume mailboxes -- the two goals pull against
  the same single number

## More Information

See ADR 0002 for the pooling mechanism and its ESS-cap addendum, and
`docs/statistics.md` for the project's broader honest-limits posture this
decision is in service of.
