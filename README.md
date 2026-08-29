# deliverability-guard

A sending circuit breaker for outbound email. Watches reputation, bounce,
complaint and warmup signals per mailbox and throttles or pauses before a
domain burns — and refuses to trip on statistically meaningless data.

**Status: pre-release, in active development.** The posterior engine, the
Instantly and Smartlead provider drivers, and the breaker/decision-log core
exist and are tested; there is no CLI, no Postmaster integration, and no
released package yet. See [BUILD-PLAN.md](BUILD-PLAN.md) for the design, the
statistical argument, and the roadmap.

The full honest-limits section, quick start, and provider capability matrix
land with the v0.1.0 release. One warning belongs here now, because it will
otherwise cause a support issue the moment someone reads the threshold
ladder in `config/thresholds.example.yml` next to a Postmaster dashboard:

> **Google's 0.3% and Amazon SES's 0.1% are NOT the same measurement, and
> this project never blends them into one number.** Google's denominator is
> Gmail inbox-delivered, DKIM-authenticated mail to engaged users. SES's is
> mail to domains that return complaint feedback to SES. The default ladder
> in `engine/breaker.py` picks threshold values informed by both, as
> provider-agnostic policy points -- not as a claim that the two rates are
> directly comparable. See `engine/breaker.py`'s module docstring for the
> full rationale.

Beyond that: this tool does not yet ship a CLI or a release, and no other
claim in this README should be read as implemented until v0.1.0.
