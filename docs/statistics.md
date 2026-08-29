# Your cold email bounce monitor is measuring 0.15 of a message

Every sending-reputation tool built on a fixed-window percentage — "alert if
the bounce rate over the last N sends exceeds X%" — has the same bug at
cold-outbound volume, and the bug cannot be tuned away by picking a better X
or a wider window. This document is why, and what `deliverability-guard`
does instead.

## The problem, worked out in full

Take a sender running 50 emails/day through one mailbox — a completely
ordinary cold-outbound volume. Gmail's hard ceiling for spam complaints is
0.3%. What does 0.3% of 50 messages look like?

```
0.3% x 50 = 0.15 messages
```

You cannot receive 0.15 of a complaint. The only things that can actually
happen in a day are:

| Complaints today | Rate | vs. the 0.3% ceiling |
|---|---|---|
| 0 | 0.0% | fine |
| 1 | 2.0% | **6.7x over** |

There is nothing in between. The metric a fixed-window breaker watches is
**quantized above the threshold it's supposed to measure** — the smallest
possible nonzero observation already blows past the ceiling by nearly 7x, and
the tool has no way to express "slightly elevated." It can only ever say
"fine" or "catastrophic," with nothing available in between, no matter how
low the true underlying rate actually is.

A breaker that watches `complaints / sends` and fires when it crosses 0.3%
will fire on that single complaint, because 2% really is greater than 0.3%.
Doing so is not merely noisy — it is **wrong in a way no amount of tuning
fixes**, because the problem isn't the threshold, it's that a single data
point at this volume carries almost no information about the true rate. To
get into a regime where 0.3% is even a *representable* observation (i.e.
where 1 complaint doesn't already overshoot it by multiples), you need
roughly 333 sends to that provider. At 50/day that's a week of accumulation
— by which point, if the tool fired on day one, it already paused a healthy
mailbox for no reason, and if it didn't fire, it was just getting lucky, not
being careful.

If you build a percentage breaker at this volume, you have built Smartlead's
Bounce Autopause with extra steps. That's not a criticism of Smartlead in
particular — it's the observation that this is what *any* fixed-window rate
threshold degenerates into once you actually run the numbers for cold
outbound.

## The fix: ask a different question

A fixed-window breaker asks: **"What fraction of my sends today were
complaints?"** — a point estimate, with no notion of how much to trust it.

The right question is: **"Given everything I've observed, how confident can
I be that the true underlying complaint rate is at or above some
threshold?"** — which requires modeling uncertainty explicitly, not just
computing a ratio and comparing it to a number.

### Beta-binomial posterior

Model the unknown true complaint rate as a random variable with a Beta
distribution. A Beta distribution is conjugate to the Binomial likelihood
(complaints out of sends is a binomial process), which means updating it
with observed data is closed-form and cheap:

```
posterior = Beta(alpha0 + complaints, beta0 + sends - complaints)
```

where `(alpha0, beta0)` are the parameters of a **prior** — what we believed
about the rate before seeing any data — and the posterior is what we believe
after.

**Why does a prior belong here at all?** A reasonable reaction, the first
time you see this, is that a prior sounds like fudging the numbers — putting
a thumb on the scale toward a favorable answer. It's the opposite. At n=1
complaint in 50 sends, there is not enough data to estimate a rate from data
alone, full stop — any method has to assume *something* about what's
plausible before it's seen evidence. The alternative to a stated, documented
prior isn't "no assumption" — it's an unstated one. A raw point estimate
(`complaints / sends`) implicitly assumes a flat prior: every rate from 0% to
100% is equally likely a priori. That is not a neutral assumption for cold
outbound email; it's a wildly implausible one, and it's exactly the
assumption that makes 1-in-50 look like a 2% mailbox instead of what it
actually is: almost no evidence either way.

`deliverability-guard`'s default prior is `Beta(0.5, 500)` — weakly
informative, centered near 0.1% (Google's own recommended target rate), with
an effective sample size around 500. That prior is:

- **Not free-floating.** It's easily overwhelmed by real data — a few
  hundred genuine complaints move the posterior substantially, and a few
  thousand real sends make the prior nearly irrelevant to the result.
- **Configurable.** Nothing about the math requires this exact prior; it's a
  documented, inspectable starting belief, not a hidden constant.
- **Not hiding anything.** It can never *permanently* mask a genuinely bad
  domain — it only slows down how fast a small amount of ambiguous evidence
  is treated as conclusive, which is exactly the property a monitor at this
  volume needs.

### Trip on the lower bound, never the point estimate

Given a posterior, `deliverability-guard` never looks at its mean (the point
estimate) to decide whether to act. It computes a **one-sided lower
confidence bound**: the value `L` such that, given everything observed,
there's a stated level of confidence (95% by default) that the true rate is
at least `L`. The breaker trips when `L` itself crosses a threshold — i.e.
only when the data supports real confidence that the rate is that high, not
merely that one ambiguous observation landed there.

At 1 complaint in 50 sends, the *mean* of the posterior sits close to a
2%-ish estimate — the same number that fools a naive breaker. The *lower
bound* is nowhere near it, because the evidence is nowhere near enough to be
confident of anything close to 2%. That gap between "what the raw number
says" and "what we can actually be confident of" is the entire fix.

### Hierarchical pooling: the low-volume problem's real answer

A single mailbox sending 50/day never accumulates enough data on its own to
say much with confidence, no matter how the math is done. But forty
mailboxes on the same domain, sending 500/day each, collectively might. The
statistically correct move is **partial pooling**: treat mailboxes on the
same domain (and domains within the same tenant) as sharing information
about the underlying reputation they're all a part of, without simply
merging their data into one undifferentiated number.

`deliverability-guard` implements this by using a group's aggregate
posterior (all mailboxes on the domain except the one being evaluated,
updated from the base prior) as the *prior* for each individual mailbox's
own posterior. The consequence is exactly the behavior you'd want from a
careful human reviewer:

- A 50-send mailbox on a domain with 40 other mailboxes running clean at
  real volume is judged *more* confidently healthy than it would be on its
  own — the domain's clean track record backs it up.
- The same 50-send mailbox, reporting a spotless 0-in-50 of its own, on a
  domain where the other 40 mailboxes are running hot, **inherits that
  risk**. Its own clean data isn't enough to prove it's the exception,
  because it isn't enough data to prove anything on its own.
- A mailbox with 5,000 sends of its own dominates its own posterior outright
  — enough of its own evidence outweighs whatever the domain says about
  everyone else.

This is the thing no fixed-window competitor does, because a fixed-window
percentage has no concept of pooling information across related streams in
the first place — it can only look at one number at a time.

## The honest limit

None of this makes low-volume monitoring statistically equivalent to
high-volume monitoring — it makes it *honest* about the difference. For
senders under roughly 1,000 messages/day/provider, `deliverability-guard` is
a leading-indicator and compliance monitor, not a statistically valid
complaint-rate breaker in the sense that a controlled experiment would
recognize. Claiming otherwise would make the tool wrong in exactly the
scenario it exists to help with. See the README's limits section, and
[docs/limits.md](limits.md) once it exists, for the full statement.

## See also

- `engine/posterior.py` — the implementation described above.
- `engine/state.py` — the `OK` / `INSUFFICIENT_DATA` / `STALE` state machine
  that keeps absence of data from ever being read as good news, which is the
  other half of not lying to yourself about what you actually know.
- `engine/changepoint.py` — sequential change detection (CUSUM), for
  catching a shift in the underlying rate faster than a fixed window could,
  once there's a real trend rather than a single ambiguous observation.
- [ADR 0002](decisions/0002-beta-binomial-hierarchical-pooling.md) — the
  decision record for this design, including what it assumes and what it
  cannot fix.
