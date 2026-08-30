---
status: "accepted"
date: "2026-08-29"
deciders: "Kate Kruger"
---

# Beta-binomial posterior with hierarchical pooling, not fixed-window rates

> **Status note (August 2026):** the pooling implemented in
> `engine/posterior.py` is complete pooling weighted by raw volume, not the
> partial pooling this ADR describes. It is not currently called from
> `breaker.evaluate()`. See ENG-4. Until that lands, every production verdict
> comes from the flat per-mailbox posterior described in ADR 0001.

## Context and Problem Statement

At cold-outbound volume (e.g. 50 sends/day/mailbox), Gmail's 0.3% complaint
ceiling corresponds to 0.15 of a message per day — a value that cannot
literally occur. The only observable daily outcomes are 0 complaints (0%) or
1 complaint (2%, 6.7x the ceiling); there is nothing representable in
between. A breaker that watches a fixed-window point estimate
(`complaints / sends`) against a fixed threshold will therefore either fire
on statistically meaningless single events or, tuned down to avoid that,
fail to distinguish a genuinely bad mailbox from a lucky one for a long time.
How should the breaker decide, at this volume, whether a mailbox's true
complaint rate is actually elevated?

## Decision Drivers

- The decision must not fire on n=1 at low volume (the project's core
  correctness requirement — see `docs/statistics.md` and
  `tests/test_posterior.py::test_posterior_refuses_to_trip_on_n1`).
- The decision must still fire, and fire promptly, once there is genuinely
  enough evidence (real volume, or a sustained shift).
- A single mailbox rarely has enough of its own data. Related mailboxes
  (same domain) and related domains (same tenant) carry information about
  each other that a per-mailbox-only method throws away.
- The result must be explainable and auditable — a decision log entry needs
  to show *why* the breaker did or didn't trip, not just a black-box score.
- No live inference infrastructure (MCMC, variational inference) — this is
  meant to run as pure, fast, dependency-light Python per evaluation.

## Considered Options

- Fixed-window percentage rate vs. a fixed threshold (the status quo every
  competitor ships)
- Wilson score interval on the raw rate
- Beta-binomial posterior, point estimate only (posterior mean vs. threshold)
- Beta-binomial posterior, trip on the lower credible bound, with
  hierarchical (partial) pooling across mailboxes/domains

## Decision Outcome

Chosen option: "Beta-binomial posterior, trip on the lower credible bound,
with hierarchical pooling," because it is the only option that both refuses
to fire on statistically meaningless single events *and* correctly uses
information from related mailboxes/domains to resolve genuinely ambiguous
cases where a single mailbox's own data can't.

A Wilson score interval would address part of the "don't fire on n=1"
problem (it's a legitimate frequentist alternative to a naive point
estimate), but it has no natural way to pool information hierarchically
across mailboxes and domains — that pooling is Bayesian by construction (a
group posterior naturally becomes an individual prior), and it's the
mechanism that solves the actual low-volume problem rather than just making
one mailbox's own estimate less overconfident.

### Consequences

- Good, because the breaker's behavior on `test_posterior_refuses_to_trip_on_n1`
  and the domain-pooling tests is provably correct, not just "seems fine in
  practice" — these are exercised as explicit properties.
- Good, because a mailbox with almost no data of its own still gets a
  meaningful, defensible verdict, by inheriting information from its domain.
- Good, because every value in a decision — prior, posterior, lower bound,
  threshold — is inspectable and can be written to the audit log verbatim.
- Bad, because this requires a `scipy` dependency, and requires anyone
  auditing the tool's decisions to understand a credible interval rather
  than eyeballing a raw percentage.
- Bad, because hierarchical pooling as implemented (see below) is a
  simplification of a full multilevel Bayesian model, not the fully correct
  version of it — see "Known limitation."

### Confirmation

`tests/test_posterior.py` encodes every required behavior as an explicit,
named test (not just table-driven cases) — most importantly
`test_posterior_refuses_to_trip_on_n1`, `test_healthy_domain_pooling_inherits_the_good_prior`,
and `test_bad_domain_pooling_inherits_the_bad_prior`. 100% branch coverage on
`engine/posterior.py` is a project requirement, enforced in CI.

## Assumption this relies on

That partial pooling via **sequential conditioning** — using a group's
aggregate posterior (all *other* members' data, updated from the base prior)
as the prior for one member's own update — is a good enough approximation of
a full hierarchical Bayesian model for this purpose. It is not identical:
a proper multilevel model would jointly estimate a shared hyperprior across
every mailbox and domain simultaneously (typically via MCMC or variational
inference), letting the amount of pooling itself be learned from how similar
the groups actually are to each other. The sequential-conditioning version
here assumes the group aggregate is a reasonable stand-in for that shared
hyperprior, which is true when group members are reasonably exchangeable
(mailboxes on the same domain behaving similarly) and gets progressively
less accurate the more heterogeneous the group actually is.

## Known limitation

**This cannot fix a genuinely absent signal.** Hierarchical pooling can
rescue a low-volume mailbox by borrowing information from its peers, but it
cannot manufacture information that doesn't exist anywhere in the group —
if an entire domain is low-volume, or a domain is brand new with no peer
history, pooling has nothing to pool from, and the correct answer is
`INSUFFICIENT_DATA` (see `engine/state.py`), not a confident-looking number
produced by leaning harder on the prior. Anyone extending this system should
resist the temptation to "solve" a data-sparse domain by tuning the prior to
be more informative — a more informative prior doesn't create real evidence,
it just makes the tool sound more confident than it has any right to be.
This is the same failure mode `docs/statistics.md`'s honest-limits section
warns about, one level up: pooling narrows the low-volume problem, it does
not eliminate it.

## Pros and Cons of the Options

### Fixed-window percentage rate vs. a fixed threshold

- Good, because it's trivial to implement and explain
- Bad, because it is structurally wrong at cold-outbound volume — this is
  the entire subject of `docs/statistics.md`

### Wilson score interval on the raw rate

- Good, because it's a well-understood, standard correction to naive
  point-estimate confidence intervals, better than a raw percentage
- Neutral, because it still requires picking a confidence level and a
  threshold, same as the chosen option
- Bad, because it has no natural hierarchical pooling mechanism across
  mailboxes/domains — it's a per-stream correction, not a way to share
  information between related streams

### Beta-binomial posterior, point estimate only

- Good, because the beta-binomial update and hierarchical pooling machinery
  is identical to the chosen option
- Bad, because using the posterior *mean* instead of its lower bound
  reintroduces the exact n=1 problem this project exists to solve — a
  point estimate is a point estimate no matter which distribution it comes
  from

### Beta-binomial posterior, trip on the lower credible bound, with hierarchical pooling (chosen)

- Good, because it solves both halves of the problem: refuses to overreact
  to noise, and correctly incorporates related mailboxes' evidence
- Bad, because it requires a `scipy` dependency and a moderately more
  sophisticated mental model to audit than "look at the percentage"

## Addendum (2026-08-30): capping the pooled effective sample size

An external audit (ENG-4) found that `pooled_prior` as originally shipped
weighted the peer group by its **raw total volume**, with no cap. That is
complete pooling, not partial pooling: a peer group's influence on an
individual mailbox's posterior grows without bound as the group grows, so a
large enough healthy peer group can dilute *any* individual mailbox's own
evidence to irrelevance, no matter how bad that mailbox's own evidence is.

Reproduction: 99 peers at 5,000 sends/0.1% complaints each, target mailbox
at 5,000 sends/5.0% complaints (16x Gmail's 0.3% ceiling). The pooled
posterior read the target as healthy -- its own weight in its own posterior
was 1%. The module-level docstring above ("a mailbox with 5,000 sends of
its own... dominates that same aggregate") was simply wrong about what the
code did.

**Fix:** `pooled_prior` and `pooled_posterior` now take a `max_ess`
parameter (`DEFAULT_MAX_POOLED_ESS = 5000.0`) that caps the peer group's
total effective sample size -- its contribution to alpha + beta -- before
it's folded into the prior. If the group's raw total volume exceeds
`max_ess`, both the total sends and total complaints are scaled down by the
same factor, which preserves the group's observed rate exactly while
bounding how much weight it can carry. This is what a full multilevel
hierarchical model would do implicitly (the shared hyperprior's own
variance limits how far any one group can pull an individual toward it);
capping the ESS is the minimal correct analogue for the sequential-
conditioning approximation this module already uses (see "Assumption this
relies on," above).

**Why 5,000:** it is not an arbitrary round number. It is the volume this
module's own tests (`test_forty_complaints_in_five_thousand_sends_does_trip`)
and `docs/statistics.md` already treat as "enough of a mailbox's own data to
be confident on its own." Capping the peer group's ESS at that same number
means a mailbox with 5,000 or more sends of its own evidence is guaranteed a
weight in its own posterior at least equal to the (bounded) peer group's,
regardless of how large that peer group grows.

**Consequence:** the legitimate use of pooling -- a mailbox with almost no
data of its own (n=1, n=10) inheriting its domain's posterior -- is
unaffected, because the cap only bounds the group's contribution, not the
individual's. `tests/test_posterior.py` encodes the corrected property set:
the original reproduction as a regression test, a monotonicity property
(more healthy peers can never mask a mailbox with enough of its own bad
evidence), an explicit ESS-cap-is-never-exceeded property, and a floor on a
mailbox's own posterior weight as the peer group grows without bound.

**Wiring:** `pooled_posterior`/`pooled_prior` had zero production callers as
of the same audit -- `engine.breaker.evaluate` computed a flat, unpooled
posterior regardless of the group's existence. `evaluate` now accepts an
optional `peer_group` parameter; when supplied, it uses the (now-capped)
pooled posterior instead. `peer_group=None` (the default) reproduces the
flat posterior every existing caller relied on before this parameter
existed. Populating a mailbox's actual peer group at each call site (from
its domain's other mailboxes) is left to the caller building the fast/slow
loop wiring -- this ADR only commits to the engine-level contract.

## More Information

See `docs/statistics.md` for the full worked example and rationale in
narrative form, and `src/deliverability_guard/engine/posterior.py` for the
implementation and its own inline documentation of each design choice.
