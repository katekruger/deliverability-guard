# deliverability-guard

A sending circuit breaker for outbound email. Watches reputation, bounce,
complaint and warmup signals per mailbox and throttles or pauses before a
domain burns — and refuses to trip on statistically meaningless data.

**Status: not started.** This repo currently holds the build plan and project
scaffolding only; no engine, provider drivers, or CLI exist yet. See
[BUILD-PLAN.md](BUILD-PLAN.md) for the design, the statistical argument, and
the roadmap.

The honest-limits section, quick start, and provider capability matrix will
land here once there is something to document truthfully. Until then: this
tool does not yet do anything, and no claim in this README should be read as
implemented.
