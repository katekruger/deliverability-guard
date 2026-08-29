# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## 0.1.0 - 2026-08-29

### Added

- `.github/workflows/release.yml`: tag-triggered release -- verifies the
  tag matches the package version and that `CHANGELOG.md` has a section for
  it, runs the full CI suite, builds, and publishes to PyPI via Trusted
  Publishing (OIDC, no API token secret anywhere) into a `release`
  environment that requires manual approval, then creates the GitHub
  Release from this section.
- `examples/demo.py` and `docs/demo.gif`: the breaker declining to trip on
  1 complaint in 50 sends, then correctly tripping on 40 complaints in
  5,000 sends -- dry-run, no credentials, same code path either way.
- README rewritten for release: the honest-limits section leads, ahead of
  every feature, per BUILD-PLAN.md §2's positioning and this project's
  entire reason for existing.
- Project scaffolding: `src/` layout, package skeleton, CI, and repo hygiene
  files, per `BUILD-PLAN.md`.
- `engine/posterior.py`: beta-binomial posterior on complaint/bounce rates,
  trip decisions on the lower credible bound (never the point estimate), and
  hierarchical partial pooling across mailboxes within a domain.
- `engine/state.py`: `OK` / `INSUFFICIENT_DATA` / `STALE` data-availability
  state machine, with the present-to-absent transition modeled as its own
  alert.
- `engine/changepoint.py`: one-sided CUSUM sequential change detection on
  the bounce/complaint stream.
- `docs/statistics.md`: why fixed-window complaint-rate breakers are
  structurally wrong at cold-outbound volume, with the worked 0.15-message
  example.
- ADR 0002: beta-binomial posterior with hierarchical pooling.
- `providers/base.py`: the `ProviderDriver` protocol, `Capability`
  declaration (`READ_STATS`/`THROTTLE`/`PAUSE`/`WEBHOOKS`), `ActionResult`
  with an explicit `UNSUPPORTED` outcome so a driver degrades to alert-only
  instead of silently no-oping, and webhook idempotency/ordering
  (`WebhookLedger`, `order_events`).
- `providers/instantly.py`: the reference provider driver -- read per-mailbox
  daily stats, pause a mailbox or campaign. No throttle primitive.
- `providers/smartlead.py`: proves the throttle path via the per-mailbox
  daily-limit endpoint; campaign-level pause only, not per-mailbox.
- `docs/threat-model.md`: the Smartlead query-string-API-key risk and how
  the driver avoids leaking it into logs or error messages.
- `engine/breaker.py`: the warn/throttle/pause ladder on the posterior lower
  bound, idempotent pause handling (no double-pause on a repeat trip,
  reconciliation on the next tick after a lost provider response), and
  `ThresholdStore` for an atomic config swap with no torn reads.
- `providers/dry_run.py`: the no-op provider decorator. Dry-run and live
  runs now produce identical decisions -- only the driver object passed to
  `engine.breaker.evaluate` differs.
- `audit/log.py`: the decision log. Every evaluation is serializable to
  JSONL and replayable from the log alone via `replay()`.
- `loops/fast.py`: webhook-driven evaluation with idempotent handling of
  redelivered events.
- `loops/slow.py`: threshold tuning, structurally unable to pause or
  throttle anything -- it has no parameter capable of it.
- ADR 0003: never auto-resume a paused mailbox, and why.
- `docs/postmaster-verdicts.md`: the Postmaster v2 verdict enums and reason
  codes, verified against the live discovery document, and confirmation
  that v2's `SPAM_RATE` dropped v1's confidence bounds -- a real regression,
  documented rather than softened.
- `signals/postmaster.py`: the Postmaster Tools v2 client --
  `domainStats:query`/`batchQuery`, `getComplianceStatus`, and the
  `create`/`getVerificationToken`/`verify` domain-onboarding flow. Gaps in
  `domainStats:query` are never coerced to zero; a 401 mid-run triggers
  exactly one token refresh and retry, never a crash; an unverified domain
  raises a clear `DomainNotVerifiedError`.
- `engine/breaker.py`: wired `getComplianceStatus` as a hard gate --
  `compliance_gate_tripped` forces `PAUSE` regardless of volume, including
  when `sends == 0`, since the compliance verdict isn't derived from
  today's send volume at all. See `signals.postmaster.forces_hard_gate`.
- `loops/slow.py`: now also tightens the ladder on `compliance_degraded`
  (Google's unsubscribe-compliance verdicts needing work), staying
  decoupled from `signals.postmaster`'s types on purpose so it never gains
  a reason to import anything capability-bearing.
- `identity/feedback_id.py`: the `campaign:segment:mailbox:tenant`
  Feedback-ID scheme, a parser, and `check_coverage` for reporting partial
  adoption as a percentage rather than silently under-attributing.
- `identity/subdomain_advisor.py`: per-campaign-class subdomain
  recommendations, honest that this is an operational requirement the tool
  can advise on but not enforce.
- `docs/limits.md`: the attribution problem -- Postmaster gives a
  domain-day scalar, the sequencer gives per-message events, there is no
  join key without the identity scheme above, and the identity scheme has
  to be adopted before an incident, not after one.
