---
status: "accepted"
date: "2026-08-29"
deciders: "Kate Kruger"
---

# Record architecture decisions

## Context and Problem Statement

This project will make a number of decisions that are expensive to reverse
and not obvious from reading the code — most notably the statistical design
in `BUILD-PLAN.md` §6. A future contributor (human or agent) who doesn't know
why a decision was made is likely to "clean it up" in a way that quietly
reintroduces the problem it solved. How do we record these decisions so the
reasoning survives past the PR that made them?

## Decision Drivers

- The reasoning behind a decision needs to outlive the PR discussion and the
  original author's memory.
- Records should be cheap enough to write that they actually get written.
- A future decision to change course should be able to reference, and
  explicitly supersede, the one it replaces — not silently overwrite it.

## Considered Options

- Architecture Decision Records (ADRs), MADR 4.0.0 format
- Design discussion left only in PR descriptions / issue threads
- A single running `DECISIONS.md` log

## Decision Outcome

Chosen option: "Architecture Decision Records (ADRs), MADR 4.0.0 format",
because it's a lightweight, version-controlled, per-decision record that
lives next to the code it governs, and MADR 4.0.0 is a widely-adopted,
non-bespoke template.

### Consequences

- Good, because decisions are discoverable in `docs/decisions/` instead of
  buried in closed PR threads.
- Good, because each ADR is small and reviewable in the same PR as the
  change it justifies.
- Bad, because it's one more file to write per non-obvious decision, and
  that discipline can lapse if not enforced.

### Confirmation

`AGENTS.md` requires an ADR in the same PR as any decision that's expensive
to reverse. Reviewers check for this.

## Assumption this relies on

That a numbered, permanent record is more likely to be read later than a PR
description, because it lives in a predictable, browsable location instead
of GitHub's PR history.

## Known limitation

ADRs record what was decided and why, not what the code currently does — the
code can drift from the decision without an obvious signal. ADRs are
backward-looking; `docs/plans/` is where forward-looking, disposable design
work lives instead.

## Pros and Cons of the Options

### Architecture Decision Records (ADRs), MADR 4.0.0 format

- Good, because it's a standard, widely-recognized format
- Good, because numbering makes ordering and superseding explicit
- Neutral, because it requires a small amount of discipline per decision

### Design discussion left only in PR descriptions / issue threads

- Good, because zero extra process
- Bad, because GitHub's search and closed-issue archaeology is a poor
  substitute for a browsable, permanent record

### A single running `DECISIONS.md` log

- Good, because everything is in one file
- Bad, because a single growing file doesn't support per-decision status
  (superseded, deprecated) as cleanly as separate numbered files

## More Information

Numbering is permanent — files are never renumbered or deleted. A decision
that reverses an earlier one is a new ADR that links back to the old one and
flips its status to `superseded by NNNN`.
