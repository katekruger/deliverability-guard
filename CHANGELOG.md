# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`cli.py`, `engine/breaker.py`: `resume` was a no-op across a restart, and
  dry-run evaluations persisted as real `PAUSED` state** (external audit
  finding CLOSE-4, the highest-severity item in the follow-up audit).
  `resume_after_human_review` wrote nothing to the decision log, so a
  restart after a resume silently rebuilt the mailbox as `PAUSED` again --
  `resume` was the *only* documented way out of `PAUSED` (ADR 0003), so
  there was in practice no way back short of hand-editing the log.
  Separately, `BreakerStateStore.from_log` never inspected a record's
  `dry_run` flag, so a dry-run deployment -- one explicitly configured to
  never touch a real mailbox -- accumulated durable `PAUSED` state anyway, a
  direct violation of AGENTS.md's dry-run non-negotiable. `cli.cmd_resume`
  now appends an `audit.log.ResumeRecord` (who resumed it, and when) that
  `from_log` replays in file order; `from_log` also now skips any decision
  record whose `dry_run` is `True`; `DecisionRecord.from_evaluation` records
  a dry-run action's outcome as the new `ActionOutcome.DRY_RUN`, distinct
  from `PERFORMED`, in the log only -- `BreakerEvaluation.action.outcome`
  itself is unchanged, preserving "dry-run must produce decisions identical
  to the live path" at the engine level. An existing-but-empty decision log
  now also raises `BreakerStateStoreLoadError` instead of being read as "no
  history yet." See the ADR 0003 addendum.

### Added

- **`loops/controller.py`, `deliverability-guard run`: the always-on
  two-loop daemon.** `run` executes `check`'s evaluation on a loop until
  stopped (`fast_interval_seconds`, default 300) and, on a much longer
  cadence (`slow_interval_seconds`, default 86400), tunes the shared
  threshold ladder from its own rolling window of recent posterior lower
  bounds via the existing (unmodified) `loops.slow.tune_thresholds`. The
  fast tick and `check` share one evaluation path
  (`loops.fast.evaluate_all_mailboxes`), so the one-shot and continuous
  forms can't drift apart. This is a polling fast loop, not a webhook
  receiver, and the slow loop's evidence is self-sourced rather than a
  live Postmaster feed -- see ADR 0004 for exactly what that does and
  doesn't implement of BUILD-PLAN.md §5's original architecture, and why.
  New config keys `fast_interval_seconds`/`slow_interval_seconds` (both
  optional, with the defaults above).
- **`cli.py`, `config.py`: a real running system** (audit finding ENG-6).
  `deliverability-guard check` is the single-shot form of the fast loop —
  the minimum viable thing a user can put in cron: it loads
  `config/thresholds.yml` (which no code previously read, despite `pyyaml`
  being a declared runtime dependency and the README's quickstart telling
  users to `cp` it into place), pulls each mailbox's stats from the
  configured provider, evaluates every mailbox through
  `engine.breaker.evaluate`, appends a decision record per mailbox, and
  exits non-zero if any mailbox's verdict isn't OK. `status <mailbox>`
  prints current breaker state; `resume <mailbox>` is the only way a
  paused mailbox becomes active again (ADR 0003). Provider credentials are
  read from the environment, never the YAML config. `[project.scripts]`
  now registers a real `deliverability-guard` entry point. The full
  always-running two-loop daemon controller (BUILD-PLAN.md §5) is still
  future work; `check` is its cron-friendly single-shot equivalent.

### Fixed

- **`engine/breaker.py`: THROTTLE was not idempotent and could reach a
  de-facto pause without ever passing through the human-review gate**
  (audit finding ENG-5a). Six identical THROTTLE evaluations halved a
  mailbox's daily limit six times (50 -> 25 -> 12 -> 6 -> 3 -> 1) without
  ever entering `PAUSED`. `MailboxBreakerStatus` gained a `THROTTLED`
  member; repeat THROTTLE verdicts are now idempotent, keyed on the
  verdict rather than the numeric limit, and a throttle that would drop
  below the floor now escalates to `PAUSE` instead of floor-clamping
  forever. See the ADR 0003 addendum.
- **`engine/breaker.py`: `BreakerStateStore` was in-memory only, so a
  process restart silently un-paused every paused mailbox** (audit finding
  ENG-5b). `BreakerStateStore.from_log(path)` now rebuilds pause/throttle
  state from `audit.log`'s append-only decision log; a log that exists but
  can't be read or parsed now raises `BreakerStateStoreLoadError` instead
  of silently falling back to an empty (every-mailbox-ACTIVE) store. See
  the ADR 0003 addendum.
- **`engine/posterior.py`: hierarchical pooling was complete pooling, not
  partial pooling, and could mask a genuinely breaching mailbox behind a
  large healthy peer group** (audit finding ENG-4). `pooled_prior` weighted
  a peer group by raw total volume with no cap, so a large enough group
  swamped any individual mailbox's own evidence regardless of how bad that
  evidence was -- reproduced as a mailbox at 16x Gmail's ceiling reading as
  healthy behind 99 healthy peers. `pooled_prior`/`pooled_posterior` now
  cap the group's effective sample size at `DEFAULT_MAX_POOLED_ESS` (5,000),
  which bounds the group's influence while leaving legitimate low-volume
  pooling unaffected. See the ADR 0002 addendum.
- `pooled_prior`/`pooled_posterior` and `engine.state.evaluate_stream` had
  zero production callers; `engine.changepoint.cusum_step` likewise.
  `engine.breaker.evaluate` now accepts an optional `peer_group` to use the
  (capped) pooled posterior; `loops.fast.evaluate_signal_with_trend` wires
  CUSUM sequential change detection alongside the breaker's own evaluation;
  `signals.postmaster.coverage_over_range` now imports `evaluate_stream`
  instead of reimplementing its transition logic.

### Changed

- `ci.yml`: expanded from a single job to a Python 3.12/3.13 matrix, now
  that the repo is public and Actions minutes aren't metered the way they
  were while private.

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
