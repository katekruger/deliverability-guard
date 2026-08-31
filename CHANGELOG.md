# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`mcp_server.py`: an MCP server wrapping the read surface** (BUILD-PLAN.md
  §4 item #27 -- "Ties into the rest of the portfolio"). Three read-only
  tools: `mailbox_status` (current breaker state), `thresholds` (the
  configured warn/throttle/pause ladder), and `recent_decisions` (recent
  decision-log entries for one mailbox, newest first). **Deliberately no
  `pause`/`resume`/`throttle` tool** -- see the [ADR 0003
  addendum](docs/decisions/0003-never-auto-resume-after-pause.md#addendum-2026-08-30-the-mcp-server-is-read-only-on-purpose):
  handing the resume decision to whatever is on the other end of an MCP
  connection is the same automatic-resume failure mode that ADR already
  rejects, just with an LLM's judgment substituted for a threshold. Each
  tool's underlying logic (`get_mailbox_status`/`get_thresholds`/
  `list_recent_decisions`) is a plain function taking `config_path`
  explicitly, testable with no MCP client or protocol layer at all;
  `build_server`'s registration wiring is confirmed via the `mcp` SDK's
  own in-process `list_tools`/`call_tool`, still with no network of any
  kind. Adds `mcp` (the official Model Context Protocol SDK) as a runtime
  dependency.
- **`providers/lemlist.py`: the lemlist driver** (BUILD-PLAN.md §3 v0.3).
  `READ_STATS` via the `activities` export, aggregated client-side into
  per-mailbox daily send/bounce counts; `PAUSE` at campaign granularity
  only (no per-mailbox pause endpoint exists), relying on lemlist's own
  documented server-side idempotency rather than adding a client-side
  guard. `THROTTLE` and `WEBHOOKS` are not claimed -- lemlist has no
  daily-limit primitive, and BUILD-PLAN.md flags its webhook support as
  unverified. Endpoint shapes are hand-authored from public documentation
  (no live lemlist account in this environment), same caveat as
  `providers/instantly.py` -- see `tests/fixtures/lemlist/README.md`.
- **`providers/apollo.py`: the Apollo driver** (BUILD-PLAN.md §3 v0.3).
  `READ_STATS` per campaign (Apollo has no global per-mailbox feed, like
  Smartlead and lemlist); `PAUSE` at campaign granularity only via
  Apollo's own named `/abort` endpoint, since Apollo's per-mailbox
  pause/throttle capability is "list only" (`list_email_accounts()`
  enumerates connected mailboxes but cannot act on one individually).
  `activate_campaign`'s resume semantics are explicitly flagged as
  unverified in both the docstring and its `ActionResult.detail`, per
  BUILD-PLAN.md's own caveat -- confirm it restores prior sequence state
  before relying on it. `THROTTLE`/`WEBHOOKS` are not claimed: no
  daily-limit endpoint exists, and Apollo's webhook support is polling
  only. Endpoint shapes are hand-authored from public documentation, same
  caveat as `providers/instantly.py` -- see
  `tests/fixtures/apollo/README.md`.
- **`signals/spamhaus.py`: Spamhaus DQS blocklist checks** (BUILD-PLAN.md
  §4 item #25 -- "the only free programmatic blocklist worth having,"
  since Talos/SenderScore/Validity have no public API). `check_ip` looks
  up an IPv4 address against the ZEN zone via a reversed-octet DNS query
  and classifies the return code (SBL/CSS/XBL/PBL, or `UNKNOWN_LISTED` for
  an unrecognized code -- never silently coerced to not-listed). A
  confirmed NXDOMAIN (`DnsNameNotFoundError`) is the only result treated
  as "not listed"; every other failure (timeout, SERVFAIL, an empty
  answer with no exception) raises `SpamhausLookupError` instead of
  reading as a clean IP -- the same missing-data-is-not-zero principle
  AGENTS.md applies everywhere else in this project. `resolve` has no
  default and must always be supplied, so a test can never fall through to
  a real DNS lookup by omission.
- **`providers/ses.py`: the Amazon SES driver** (BUILD-PLAN.md §3 v0.3 --
  "the only platform with native breaker primitives" among the ESPs not
  yet implemented). `READ_STATS` via CloudWatch's `Send`/`Bounce` metric
  sums, dimensioned by configuration set (SES has no per-mailbox concept
  at all -- see the module docstring); `PAUSE` via SESv2
  `PutConfigurationSetSendingOptions` (a configuration set, reachable
  through the generic `pause()`) and a separate, deliberately-not-generic
  `pause_account()` account-wide kill switch, which is never reachable
  through `pause()` itself -- the same "a disproportionate action must be
  deliberate" rule `providers/smartlead.py` already established for
  campaign-vs-mailbox pause. `THROTTLE` is not claimed: SES's rate control
  is sends-per-second, not a daily-volume limit, and mapping one onto the
  other would misrepresent what actually happens. `WEBHOOKS` is not
  claimed either -- SES delivers bounce/complaint notifications via SNS, a
  push mechanism this driver does not implement (a documented known
  limitation, not silently missing).

  This is the first dependency-adding driver in the project: `boto3` is a
  new runtime dependency, justified in [ADR 0005](docs/decisions/0005-boto3-dependency-for-ses.md)
  as avoiding hand-rolled AWS SigV4 request signing (security-sensitive
  cryptographic code this project shouldn't reimplement) at the cost of a
  genuinely heavy (~15MB) addition. Tests use hand-defined `Protocol`
  fakes for the SESv2/CloudWatch clients -- no `moto`, no live AWS
  account, no network call of any kind.
- **`signals/dmarc.py`: DMARC aggregate-report auth-health signal, built
  on `parsedmarc`** (BUILD-PLAN.md §4 item #17 -- "Reuse as a library. Do
  not reimplement."). `summarize_auth_health` aggregates parsedmarc's own
  parsed-report dicts (this module never parses raw DMARC XML itself)
  into total/aligned message counts and a ranked list of unauthenticated
  `UnknownSource`s -- sources failing BOTH SPF and DKIM alignment (RFC
  9989 §3: DMARC passes on either, not both). Zero reports is
  `INSUFFICIENT_DATA`, never a 0%- or 100%-aligned rate. A malformed
  report (missing `records`/`alignment`/`source`, a non-integer `count`)
  raises `MalformedDmarcReportError` rather than silently skipping or
  defaulting -- same missing-data discipline as everywhere else in this
  project. Explicitly a slow-loop, cross-provider signal, never a
  real-time one, and carries no complaint or inbox-placement data at all.

  Adds `parsedmarc` as a runtime dependency, justified in [ADR
  0006](docs/decisions/0006-parsedmarc-dependency-for-dmarc-auth-health.md)
  -- BUILD-PLAN.md's own research already concluded this shouldn't be
  reimplemented; the ADR discloses the ~20-transitive-package weight this
  adds. `tests/test_dmarc.py` includes a real integration test against
  `parsedmarc.parse_aggregate_report_xml(..., offline=True)` (no network
  call) that caught a real gap in the initial implementation: offline
  parsing leaves `source.base_domain` unset, which is why
  `_source_identifier` falls back to the raw IP address.
- **`identity/warmup_advisor.py`: warmup curve adherence, explicitly
  labeled as folklore** (BUILD-PLAN.md §4 item #19). `check_adherence`
  compares a mailbox's actual observed daily sends against a piecewise-
  linear-interpolated ramp curve and classifies the result as
  `ON_SCHEDULE`/`AHEAD_OF_SCHEDULE`/`BEHIND_SCHEDULE` (within a
  configurable `tolerance` fraction) or `PAST_CURVE` once the mailbox is
  older than the curve's last defined checkpoint -- deliberately not a
  "bad" outcome, since the curve has nothing more to recommend past that
  point. `DEFAULT_WARMUP_CURVE` is documented, per BUILD-PLAN.md §8's own
  instruction, as vendor consensus with no RFC, M3AAWG document, or
  independent research behind it -- "presenting folklore as authoritative
  is the project's most likely credibility failure" -- and every function
  accepts a `curve` override so a caller is never stuck with it. No
  enforcement of any kind: this is a comparison against a baseline with no
  ground truth, never a verdict.
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

- **`cli.py`: only Instantly was selectable, and a transport failure
  tracebacked with the same exit code as "ran fine, found a breach"**
  (external audit finding CLOSE-5). `build_driver` now also registers
  `smartlead` (via the new `providers.smartlead.SmartleadCampaignDriver`,
  which pins Smartlead's per-campaign statistics endpoint to one
  `SMARTLEAD_CAMPAIGN_ID` so it satisfies the generic `ProviderDriver`
  Protocol) and `noop` (`providers/noop.py`, a credential-free driver that
  reports no mailboxes, so the CLI's own wiring can be exercised end to
  end without a live account — previously only possible by calling
  `cmd_check` directly with a Python `FakeDriver`). `main()` now catches
  `httpx.HTTPError`/`providers.base.ProviderError` around `check`/`run` and
  exits `3` with a clean message instead of a raw traceback and exit `1`.

- **`engine/breaker.py`: pooling could still make the breaker LESS sensitive
  than evaluating a mailbox alone, at moderate own-volume** (external audit
  finding CLOSE-2, a follow-up on the ESS-cap fix below). Between roughly 91
  and 389 own sends, a mailbox breaching at 5% (16x Gmail's ceiling) on its
  own evidence alone read as healthy once pooled with a large, healthy
  peer group -- the ESS cap bounds how much weight the group carries, but
  not how far it can pull the posterior itself. `evaluate()` now takes the
  worse (higher) of the pooled and flat lower bounds whenever `peer_group`
  is given: pooling can only ever add sensitivity, never remove it. See
  [ADR 0007](docs/decisions/0007-pooling-never-reduces-breaker-sensitivity.md).

- **`loops/fast.py`: `pooled_posterior` and `cusum_step` had callers that
  nothing itself called in production** (external audit finding CLOSE-1).
  `evaluate_all_mailboxes` -- the shared chokepoint `cli.cmd_check` and
  `loops.controller.run`'s fast tick both use -- called `engine.breaker.evaluate`
  with neither `peer_group` nor a CUSUM state, so a real `check`/`run` never
  pooled and never ran the trend check, even though both mechanisms were
  themselves correct. `evaluate_all_mailboxes` now builds each mailbox's
  same-domain peer group (leave-one-out) and threads it through, and
  accepts an optional `cusum_states` mapping so `cusum_step` runs alongside
  the breaker's own posterior ladder every tick when a caller opts in
  (`loops.controller.run` now does, persisting state for the daemon's
  lifetime). `max_pooled_ess` is now a configurable value
  (`config/thresholds.yml`'s `max_pooled_ess`). The zero-caller
  `evaluate_signal_with_trend` was deleted rather than adopted for this --
  CUSUM's per-period model fits this pull-based tick, not a per-webhook
  signal nothing in this codebase receives yet. `signals.postmaster.
  coverage_over_range`, the third function named in the same finding, moved
  to `experimental.postmaster_coverage`: there is no Postmaster ingestion
  pipeline anywhere in this codebase to wire it into yet, unlike
  `get_compliance_status`/`forces_hard_gate`, which already feed a real
  integration point. See the ADR 0002 addendum.
- **`loops/fast.py`, `engine/breaker.py`: THROTTLE never reached the
  provider on the real `check`/`run` path at all** (CLOSE-3a, the same
  wiring gap as above). `evaluate_all_mailboxes` never passed
  `current_daily_limit`, so every THROTTLE-worthy evaluation reported
  itself `UNSUPPORTED` instead of actually reducing a mailbox's daily
  limit. `providers.base.MailboxDayStats` gained an optional
  `current_daily_limit` field; `loops.fast.aggregate_mailbox_stats` takes
  each mailbox's most recently reported value and `evaluate_all_mailboxes`
  passes it through.

- **`engine/breaker.py`: the THROTTLE rung latched, reopened, and never
  auto-recovered** (external audit finding CLOSE-3, points 3b-3d; 3a is the
  same wiring fix as the `evaluate_all_mailboxes` commit below). Three
  separate bugs, all in `_act`/`evaluate`: (1) once THROTTLED, nothing ever
  cleared the status back to `ACTIVE` on recovery, and idempotency was keyed
  purely on status rather than on the mailbox's actual daily limit, so
  `THROTTLE -> OK -> THROTTLE` never reached the provider a second time, and
  a human manually restoring a throttled limit was invisible to the
  idempotency check; (2) a FAILED pause attempt was marked `ACTIVE`, the
  same as a never-touched mailbox, so a subsequent THROTTLE re-halved an
  already-throttled limit (25 -> 12) -- reopening the exact cascade the
  ENG-5a fix above exists to prevent; (3) the floor-escalation guard used
  `<` instead of `<=`, so a daily limit of 2 or 3 halved to exactly 1 (the
  floor) without ever escalating to `PAUSE`, silently clamping to a
  de-facto pause with no human gate. `evaluate()` now clears `THROTTLED` to
  `ACTIVE` on a sustained `OK` verdict; `BreakerStateStore` now tracks the
  daily limit a throttle was last applied against and keys idempotency on
  it; `MailboxBreakerStatus` gained `PAUSE_FAILED`, distinct from `ACTIVE`,
  which preserves that limit memory through a failed pause attempt; the
  floor guard is now `<=`. See the ADR 0003 addendum.

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
  (capped) pooled posterior, and `signals.postmaster.coverage_over_range`
  now imports `evaluate_stream` instead of reimplementing its transition
  logic. (A follow-up audit, CLOSE-1, found neither `peer_group` nor
  `cusum_step` actually had a caller in `check`/`run` yet at this point --
  see the CLOSE-1 entry above for the wiring that closed that gap, which
  superseded and removed the `evaluate_signal_with_trend` function this
  entry originally described.)

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
