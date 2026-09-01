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

- **`engine/breaker.py`: a PAUSED mailbox auto-un-paused through THROTTLE
  then OK, with no human review** (external audit finding CLOSE5-1 — the
  serious one). `_act`'s THROTTLE branch never consulted `state_store.
  status_of`, unlike its PAUSE branch, which has always checked "is this
  mailbox already paused?" before acting. A mailbox already PAUSED whose
  evidence on a later tick happened to land in the THROTTLE band got a REAL
  `driver.throttle()` call, `mark_throttled` moved it to THROTTLED, and one
  later OK evaluation then hit CLOSE-3b's sustained-recovery path and moved
  it all the way to ACTIVE — `resume_after_human_review` never called, no
  `ResumeRecord` ever written. Reproduction: seven separate `check`
  processes ended with the breaker having paused a mailbox, then told the
  provider to re-enable its sending at a reduced limit, then reported it
  fully healthy three runs later — contradicting ADR 0003 itself,
  `BreakerStateStore`'s own class docstring ("`resume_after_human_review`
  is the ONLY path back from PAUSED to ACTIVE"), and `mark_active`'s own
  docstring ("never for un-pausing a confirmed-paused mailbox on its own").
  Fixed by adding the same status check to `_act`'s THROTTLE branch that
  its PAUSE branch already has (see [ADR
  0008](docs/decisions/0008-throttle-must-not-act-on-a-paused-mailbox.md)
  for why the check lives there and not in `evaluate()`), and extending
  `from_log`'s THROTTLE/PERFORMED replay to recognize the resulting
  paused-idempotent no-op record and leave status untouched during replay.
  A new source-level guard test
  (`test_act_checks_paused_status_before_ever_calling_throttle`) asserts
  `_act` consults PAUSED status before it can ever reach
  `driver.throttle()`, in the same idiom the repo already used for the
  symmetric `resume_after_human_review` claim.

- **`engine/breaker.py`: `from_log`'s PAUSE/`UNSUPPORTED` branch didn't
  mirror `mark_active`, leaving a stale `throttled_at_limit` behind**
  (external audit finding CLOSE5-2, found by extending the CLOSE4-1
  permutation sweep with a `PAUSE_UNSUPPORTED` move and switching from
  `itertools.permutations` to `itertools.product` so repeated moves are
  covered — 14 of 343 three-move sequences mismatched). `_act`'s
  PAUSE/UNSUPPORTED path calls `state_store.mark_active`, which clears both
  `_throttled_at_limit` and the unsupported-throttle streak; `from_log`'s
  corresponding `else` branch set status to ACTIVE but left
  `throttled_at_limit` in place. The stale limit then made the very next
  `THROTTLE`/`PERFORMED` record look like CLOSE4-1's `is_idempotent_replay`
  case, so replay never restored THROTTLED — reachable in production on
  `smartlead` (CLI-selectable, `pause(MailboxRef)` UNSUPPORTED,
  `throttle(mailbox_id, limit)` PERFORMED): on identical evidence, `run`
  (no restart) and `check` (restart between every evaluation) made a
  DIFFERENT number of real provider calls, and after any restart the
  breaker read a genuinely throttled mailbox as pristine. Fixed with the
  one-line mirror of `mark_active` the finding named
  (`throttled_at_limit.pop(mailbox, None)`). The permutation sweep now
  compares `status_of`, `throttled_at_limit`, AND the unsupported-throttle
  streak (previously status only), and 0/343 sequences mismatch. A new
  `test_daemon_and_cron_agree_on_a_smartlead_shaped_three_move_sequence`
  reproduces the finding through the real `evaluate()`/`cli.main` paths
  directly. Also corrected: the comment above the fixed branch, which
  argued it was "structurally unreachable from an already-PAUSED mailbox"
  — true for the status question, irrelevant to the limit question, since
  this defect's reproduction never starts from PAUSED — and this
  CHANGELOG's own CLOSE4-1 entry, which claimed a non-acting record "leaves
  status (and, where relevant, `throttled_at_limit`) untouched during
  replay, exactly mirroring `_act`." It didn't, for exactly this branch.

- **`README.md`/`CHANGELOG.md`: two absolute claims with no executable
  guard** (external audit finding CLOSE5-3). Both landed in the CLOSE4-3
  commit; neither was false when written, and neither would have failed
  when it became false. (1) The README's/CHANGELOG's claim that the
  ladder "only ever constructs a `MailboxRef`, never a `CampaignRef`" —
  added `test_engine_breaker_module_never_constructs_a_campaign_ref`, the
  same source-level idiom `tests/test_breaker.py` already uses for the
  `resume_after_human_review` claim. (2) The CLOSE4-1 CHANGELOG entry's
  "exactly mirroring `_act`" — refuted by CLOSE5-2 above; corrected in
  that same commit. Swept the README for the same shape of claim while
  here: "every driver's `pause()`/`throttle()` is always callable and
  returns an explicit 'unsupported' result rather than silently doing
  nothing" (line 101) also had no guard — added
  `test_every_driver_declines_an_unsupported_capability_without_raising`
  to `tests/test_provider_conformance.py`, exercising only the (driver,
  verb) pairs each driver structurally declines, so it never makes a live
  call against the capabilities the HTTP-backed drivers actually implement.

- **The log-growth cycle on a paused mailbox** (external audit finding
  CLOSE5-4 — verified, not assumed, to be a side effect of CLOSE5-1's fix
  rather than a separate defect). Before CLOSE5-1, ten consecutive `check`
  processes against a provider with no daily limit escalated to PAUSE,
  un-paused via the CLOSE5-1 bug, and re-escalated with period 4 forever —
  a real provider `pause()` call every four runs, and an unbounded, cyclic
  decision log. Re-running the same ten-process harness after CLOSE5-1:
  the mailbox reaches PAUSED once and stays there, with exactly one real
  provider `pause()` call — the cycle is gone, as a direct consequence of
  the mailbox's status no longer being un-paused mid-cycle. The log still
  grows one record per `check` invocation (a defensible audit trail, per
  the finding's own framing), but every record for an already-PAUSED
  mailbox now carries CLOSE5-1's own "mailbox is paused; ... refused
  pending human review" detail rather than reporting a bare, unexplained
  `THROTTLE` — an honest record, not a misleading one.

- **`README.md`: the capability matrix's "campaign only" pause marks
  described driver-API surface the breaker itself never reaches** (external
  audit finding CLOSE4-3, a documentation decision rather than a bug).
  `engine.breaker.evaluate`'s ladder only ever constructs a `MailboxRef`,
  never a `CampaignRef` — so through the CLI, PAUSE actually executes
  against exactly one provider (`instantly`) and THROTTLE against exactly
  one (`smartlead`); no single provider can do both, and Lemlist/Apollo/
  SES's campaign-level pause methods, while real and tested, are
  unreachable from the breaker's own evaluation loop. Added a paragraph to
  the README explaining this explicitly, rather than teaching the ladder to
  pause a `CampaignRef` (real, undecided v0.2 scope — pausing an entire
  campaign to handle one mailbox's bad evidence is a disproportionate
  action this project has not decided the breaker should take
  automatically).

- **`engine/breaker.py`: `from_log` silently un-paused a PAUSED mailbox**
  (external audit finding CLOSE4-1, and the cause of CLOSE4-2's
  never-terminating escalation cycle). Three of `_act`'s branches never
  touch `state_store` on the live path — a THROTTLE verdict with an
  UNSUPPORTED or FAILED outcome, and a THROTTLE verdict whose PERFORMED
  outcome was actually `_act`'s own idempotent no-op — but `from_log`
  unconditionally set `status[mailbox] = ACTIVE` (or, for the idempotent
  PERFORMED case, `THROTTLED`) for all three, silently overwriting a
  PAUSED status a `PAUSE`/`PERFORMED` record earlier in the log had put
  behind the human-review gate (ADR 0003) — contradicting the gate itself,
  `from_log`'s own docstring, and README.md:58. Reproduction: ten
  unattended `check` runs against a provider that reports no daily limit
  escalated to PAUSE via CLOSE3-2's streak on run 4, then a
  THROTTLE/UNSUPPORTED record on run 5 un-paused it, and the
  escalate-then-un-pause cycle repeated with period 4 forever — a real
  provider `pause()` call every time it escalated, not once. Fixed: a
  record whose action did not touch the provider now leaves status (and,
  where relevant, `throttled_at_limit`) untouched during replay for
  THROTTLE's three branches, mirroring `_act` for those three cases
  specifically — **not** "exactly mirroring `_act`" in general, a claim
  CLOSE5-2 (below) found false for the PAUSE/`UNSUPPORTED` branch this
  entry didn't touch. The idempotent-PERFORMED case is distinguished from a
  genuine throttle purely from records already replayed: a genuine
  throttle always sets `throttled_at_limit` for the first time or to a
  STRICTLY larger value, while an idempotent replay's `applied_daily_limit`
  never exceeds what's already tracked. A new property-style test
  (`test_from_log_replay_matches_the_live_path_over_every_move_ordering`)
  asserts replayed status equals live-path status over all 720 orderings of
  six representative moves (PAUSE/PERFORMED, THROTTLE/PERFORMED,
  THROTTLE/UNSUPPORTED, THROTTLE/FAILED, OK, RESUME) — the invariant meant
  to stop this exact defect shape recurring a fifth time. Ten consecutive
  `check` processes against a provider with no daily limit now reach PAUSED
  once, on run 4, and stay there through run 10, with exactly one real
  provider pause call.

- **`cli.py`/`README.md`: two small documentation corrections** (external
  audit finding CLOSE3-6). `build_parser()` set no `epilog`, so `--help`'s
  rendered text had no exit-code content at all, despite a commit message
  claiming exit codes were documented in "the module docstring, README, and
  `--help`'s exit code map" — the module docstring and README were correct;
  `--help` wasn't. Added an `epilog` with the exit code map, the one place a
  cron author actually looks. README line 151 said warmup adherence was
  "Not implemented in v0.1" without qualification; `identity/
  warmup_advisor.py` (now `experimental/warmup_advisor.py` — CLOSE3-5) ships
  a complete, tested implementation with no caller. The line now says what's
  actually true: not implemented in the shipped surface, but a real
  heuristic exists, quarantined because nothing calls it yet.

- **Three functions with zero production callers, ten unimported modules,
  and an unwired Postmaster hard gate** (external audit finding CLOSE3-5 --
  the fourth round of the same finding on `loops.fast.evaluate_signal`,
  `engine.state.evaluate_stream`, and `identity.warmup_advisor.
  check_adherence`). `evaluate_signal`/`FastLoopSignal` moved to the new
  `experimental.webhook_signal` (nothing in this codebase accepts an
  inbound webhook yet). `evaluate_stream`/`DailyReport`/`classify`/
  `StateEvaluation` moved to the new `experimental.state` (their only
  production caller, `experimental.postmaster_coverage`, is itself
  experimental — a production function whose sole consumer is
  non-production belonged on one side or the other; `DataState` alone
  stays in `engine/state.py` on the strength of `engine.breaker`/
  `audit.log`'s real usage). `identity.warmup_advisor` moved to
  `experimental.warmup_advisor` wholesale. `mcp_server.py` gained a real
  `main()` and a `deliverability-guard-mcp` console script entry — it
  previously had no caller anywhere outside its own test file. Most
  importantly: `loops.fast.evaluate_all_mailboxes` gained
  `compliance_gate_tripped_for`, so `signals.postmaster.forces_hard_gate`'s
  verdict can now actually reach the shared `check`/`run` chokepoint
  instead of only `engine.breaker.evaluate` called directly — the gate
  itself remains unconnected to any live Postmaster OAuth/domain source
  (real, separately-scoped setup), documented as such rather than silently
  left as a hidden gap. A new `tests/test_reachability.py` runs a real
  `check` in a fresh subprocess and asserts every module outside
  `experimental/` is either imported by it or explicitly named with a
  reason (a separately-wired entry point, a directly-callable library
  utility per README's own "full public surface" description, or the
  documented-unwired Postmaster gate) — so a fifth round of this same
  finding fails a test instead of needing another audit to notice.

- **`cli.py`/`providers/{lemlist,apollo,ses}.py`: three implemented drivers
  weren't CLI-selectable, and the README contradicted itself about it**
  (external audit finding CLOSE3-4). `LemlistDriver`/`ApolloDriver`/
  `SesDriver` each pin `read_mailbox_stats` to a required `campaign_id` (or,
  for SES, `configuration_set_name`) keyword the generic `ProviderDriver`
  Protocol has no room for, so `build_driver("lemlist"|"apollo"|"ses")`
  raised `unknown provider` — while README rows for all three said
  "Implemented" under a "Status in this repo" heading that reads as
  availability. New `LemlistCampaignDriver`/`ApolloCampaignDriver`/
  `SesConfigurationSetDriver` adapters apply the same pinning pattern
  `SmartleadCampaignDriver` already established, registered in
  `build_driver` behind `LEMLIST_API_KEY`/`LEMLIST_CAMPAIGN_ID`,
  `APOLLO_API_KEY`/`APOLLO_CAMPAIGN_ID`, and
  `SES_CONFIGURATION_SET_NAME`/`AWS_REGION` respectively (`SesDriver` gained
  an explicit `region_name` parameter so `build_driver` can construct it
  deterministically instead of depending on ambient AWS config). A new
  `tests/test_provider_conformance.py` asserts every CLI-selectable driver
  satisfies `ProviderDriver`, so pyright catches the next divergence.
  `noop` now reports a small synthetic two-mailbox fixture instead of an
  empty list, so `check`/`run` genuinely exercise aggregation, evaluation
  (including hierarchical pooling), and decision-log writing — README line
  62's claim that it did was previously false; no log file was even created.

- **`providers/smartlead.py`/`engine/breaker.py`: the THROTTLE rung was
  unreachable against every real, CLI-selectable provider** (external audit
  finding CLOSE3-2). `current_daily_limit` plumbing was correct end to end,
  but zero of five shipped drivers ever populated
  `MailboxDayStats.current_daily_limit` — including `smartlead`, the one
  driver declaring `Capability.THROTTLE`, so every real THROTTLE verdict
  read `action_outcome: UNSUPPORTED` regardless of configuration.
  `SmartleadCampaignDriver`'s statistics parsing now reads a row's own
  current limit back from `message_per_day`, the same field its `throttle()`
  request body writes to. Separately, a THROTTLE verdict a provider
  genuinely cannot execute (unknown limit, or no throttle primitive at all)
  no longer writes an identical UNSUPPORTED record forever: after
  `_MAX_UNSUPPORTED_THROTTLE_STREAK` (3) consecutive unexecutable throttles
  for one mailbox, the verdict escalates to PAUSE through the human-review
  gate (ADR 0003). The streak persists across restarts the same way
  CLOSE3-1's applied-limit memory does.

- **`engine/breaker.py`/`cli.py`: nothing could clear a persisted THROTTLED
  mailbox** (external audit finding CLOSE3-3). `from_log` left OK and WARN
  verdicts alone entirely, so a mailbox that recovered (THROTTLE, then
  sustained OK evaluations) but happened to restart mid-recovery read
  THROTTLED forever, and `resume` refused it outright ("is not paused;
  nothing to resume") with nowhere to go from there. `from_log` now honours
  an OK verdict recorded after a THROTTLE as a recovery, mirroring
  `evaluate()`'s own sustained-recovery check, clearing both status and the
  remembered applied limit together. `resume` now also accepts a THROTTLED
  mailbox (not just PAUSED), for the case where the mailbox's own evidence
  never recovers on its own and a human needs an explicit way back to
  ACTIVE.

- **`engine/breaker.py`/`audit/log.py`: `from_log` forgot the daily limit it
  had just applied, so a process that restarts between every evaluation
  (e.g. `check` run from cron) re-halved on every single invocation instead
  of staying idempotent** (external audit finding CLOSE3-1 — "the cron
  cascade"). Six identical `check` invocations against one mailbox used to
  compound `50 -> 25 -> 12 -> 6 -> 3 -> PAUSE`; in-process they correctly
  stayed at one throttle call, because `BreakerStateStore._throttled_at_limit`
  survived within a single process but was never persisted. `DecisionRecord`
  now carries `applied_daily_limit` for a PERFORMED throttle, and
  `BreakerStateStore.from_log` restores `_throttled_at_limit` from it
  alongside status — six separate `check` invocations now produce exactly
  one provider throttle call and a final limit of 25.

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
