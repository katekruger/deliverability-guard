# deliverability-guard

[![CI](https://github.com/katekruger/deliverability-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/katekruger/deliverability-guard/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/deliverability-guard.svg)](https://pypi.org/project/deliverability-guard/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A sending circuit breaker for outbound email — it watches reputation, bounce and complaint signals per mailbox and throttles or pauses before a domain burns, and it refuses to trip on statistically meaningless data. Run `deliverability-guard run` to watch continuously, or `check` on a cron schedule for the same evaluation one-shot.

## The honest limits

> For senders under roughly 1,000 messages/day/provider, this is a leading-indicator and compliance monitor, not a statistically valid complaint-rate breaker. Overclaiming here would make it wrong in production.

That sentence is a credibility asset, not a hedge. It stays here, first, on purpose.

At cold-outbound volume — which is most of the volume this tool is built for — a single complaint moves the observed rate by amounts a fixed-window breaker cannot interpret honestly. This tool models that uncertainty explicitly instead of pretending it isn't there. See [`docs/statistics.md`](docs/statistics.md) for the full argument and [`docs/limits.md`](docs/limits.md) for what the underlying data itself can't tell you, no matter how the statistics are done.

## The 0.15-of-a-message problem

A sender running 50 emails/day through one mailbox — an entirely ordinary cold-outbound volume. Gmail's hard ceiling is a 0.3% spam rate.

```
0.3% × 50 = 0.15 messages
```

You cannot receive 0.15 of a complaint. The only things that can actually happen in a day are 0 complaints (0%) or 1 complaint (2% — 6.7× over the ceiling). There is nothing representable in between. A breaker that watches the raw percentage will fire on that single complaint, because 2% really is greater than 0.3% — and it will be *wrong* to do so, because one data point at this volume carries almost no information about the mailbox's true underlying rate.

`deliverability-guard` never looks at the raw percentage. It models the unknown true rate as a Bayesian posterior and trips on the **lower bound of a 95% credible interval**, not the point estimate — the same one complaint in 50 sends that reads as "2%, over the limit, pause it" to a naive tool reads as "nowhere near enough evidence" here. See [`docs/statistics.md`](docs/statistics.md) for the full worked example, and run it yourself:

```bash
uv run python examples/demo.py
```

![The breaker declining to trip on 1 complaint in 50 sends, then correctly tripping on 40 complaints in 5,000 sends](docs/demo.gif)

## Quick start (dry-run)

Dry-run is the default everywhere in this project. It is not a separate code path — it's the exact same evaluation logic, with a no-op decorator standing in for the real provider call, so what you see in dry-run is what would actually happen.

```bash
git clone https://github.com/katekruger/deliverability-guard
cd deliverability-guard
uv sync
cp .env.example .env                            # fill in your provider API key
cp config/thresholds.example.yml config/thresholds.yml
uv run deliverability-guard check                # evaluate every mailbox once, print verdicts
```

`check` is the single-shot form of the fast loop — the smallest thing you can put in cron to get a running system, not just a library. It reads `config/thresholds.yml`, pulls each mailbox's stats from the provider named there, evaluates every one through the exact same `engine.breaker.evaluate` shown below, appends a decision record per mailbox to the decision log, and exits non-zero if any mailbox's verdict isn't `OK` — so a cron job's failure alerting just works.

`run` is the always-on form: the same evaluation, on a loop, forever (Ctrl-C to stop).

```bash
uv run deliverability-guard run                  # watches continuously until stopped
```

Every `fast_interval_seconds` (default 300 = 5 minutes) it re-pulls stats and re-evaluates every mailbox, exactly like `check` — they share one code path (`loops.fast.evaluate_all_mailboxes`), so the two can't drift apart. Every `slow_interval_seconds` (default 86400 = 24h) it looks at its own recent evidence and tightens the ladder if the last several evaluations have been creeping toward `warn` without crossing it — the same "your thresholds were too loose for the last few days" logic BUILD-PLAN.md §5 describes, sourced from the daemon's own history rather than a live Postmaster feed. See [ADR 0004](docs/decisions/0004-polling-daemon-and-self-sourced-slow-loop-evidence.md) for exactly what this does and doesn't implement yet — most importantly, this polls on an interval rather than receiving pushed webhooks.

`status <mailbox>` prints a mailbox's current breaker state, and `resume <mailbox>` is the only way a paused mailbox becomes active again (see [ADR 0003](docs/decisions/0003-never-auto-resume-after-pause.md) — there is no automatic path back from `PAUSED`, whether it was `check` or `run` that paused it).

Exit codes: `0` all clear, `1` `check` found a breach (or `resume` was refused), `2` a config/setup error (bad YAML, unknown provider, missing credential), `3` a provider transport failure (network error, rate limit exhausted, malformed response) — distinct from `1` so a cron wrapper can tell "the fleet is healthy" apart from "we couldn't even ask the provider."

Selectable providers: `instantly` (needs `INSTANTLY_API_KEY`), `smartlead` (needs `SMARTLEAD_API_KEY` and `SMARTLEAD_CAMPAIGN_ID` — its stats endpoint is per-campaign, not global), and `noop`, which needs no credentials at all and reports no mailboxes — set `provider: noop` in `config/thresholds.yml` to exercise `check`/`run` end to end (config loading, the decision log, exit codes) without a live account.

```bash
uv run deliverability-guard status sender@yourdomain.com
uv run deliverability-guard resume sender@yourdomain.com   # only after a human has looked
```

To call the engine directly instead of through the CLI:

```python
from datetime import UTC, datetime

from deliverability_guard.engine.breaker import DEFAULT_LADDER, BreakerStateStore, evaluate
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.providers.base import MailboxRef
from deliverability_guard.providers.instantly import InstantlyDriver

driver = InstantlyDriver(api_key="...")  # never contacted while dry_run=True
mailbox = MailboxRef(provider="instantly", mailbox_id="sender@yourdomain.com")

result = evaluate(
    driver=driver,
    mailbox=mailbox,
    sends=50,
    complaints=1,
    prior=DEFAULT_PRIOR,
    thresholds=DEFAULT_LADDER,
    state_store=BreakerStateStore(),
    dry_run=True,  # no code path can pause or throttle without dry_run=False, set on purpose
    now=datetime.now(UTC),
)

print(result.verdict)  # Verdict.OK -- one complaint in 50 sends isn't evidence of anything yet
```

The full public surface is the CLI (`cli.py`) plus the `engine`, `providers`, `signals`, and `identity` modules directly; `examples/demo.py` is the fastest way to see the engine run without any provider credentials at all.

## Provider capability matrix

Capability declaration is the whole design of the provider driver interface (`providers/base.py`): of nine sequencer platforms surveyed, **two cannot pause anything at all**. An interface that assumed every provider could pause would already be wrong for close to half of what was surveyed. Every driver's `pause()`/`throttle()` is always callable and returns an explicit "unsupported" result rather than silently doing nothing when a provider lacks a capability.

| Provider | Status in this repo | Read stats | Pause | Throttle | Bounce webhook |
|---|---|:--:|:--:|:--:|:--:|
| **Instantly** | ✅ Implemented (reference driver) | per-mailbox daily | ✅ mailbox or campaign | ❌ no primitive | yes |
| **Smartlead** | ✅ Implemented and CLI-selectable (proves the throttle path) | per-campaign | campaign only, not per-mailbox | ✅ per-mailbox daily limit | yes |
| **Lemlist** | ✅ Implemented | activities export, aggregated per mailbox/day | ✅ campaign only, idempotent server-side | ❌ no primitive | unverified (capability not claimed) |
| **Apollo** | ✅ Implemented | per-campaign daily stats | ✅ campaign (`/abort`, resume semantics unverified) | ❌ no primitive | polling only (capability not claimed) |
| Outreach | Researched, not yet implemented | yes | likely (unverified) | likely (unverified) | best-in-class |
| Salesloft | Researched, not yet implemented | yes | **likely UI-only** — no API | ❌ | unverified |
| Amplemarket | Researched, not yet implemented | ❌ | **no status-change API at all** — app-only | ❌ | ❌ |
| **Amazon SES** | ✅ Implemented (read + pause; see [ADR 0005](docs/decisions/0005-boto3-dependency-for-ses.md)) | CloudWatch `Send`/`Bounce` metrics | ✅ configuration set or whole account | ❌ no daily-volume primitive | not implemented (SNS ingestion is separate infra) |
| Postmark | Researched, not yet implemented | yes | ❌ no pause primitive | ❌ | yes |
| SendGrid | Researched, not yet implemented | yes | ❌ no pause primitive | ❌ | yes |

Instantly is the reference implementation because it's the only surveyed vendor exposing both per-mailbox daily bounce data *and* per-mailbox pause — the minimum viable substrate for a circuit breaker. Smartlead's per-mailbox daily-limit endpoint is, in this project's view, the most underrated endpoint in the space: it's the difference between a circuit breaker and a kill switch, which is why it's the second driver built rather than a third pause-only one.

## Thresholds

The default ladder (`config/thresholds.example.yml`, `engine/breaker.py`), keyed on the posterior's lower confidence bound, never the point estimate:

| State | Threshold | Action |
|---|---|---|
| Warn | ≥ 0.05% | Notify only |
| Throttle | ≥ 0.10% | Reduce daily limit 50% |
| Pause | ≥ 0.20% | Pause the mailbox |

Sourced against:

| Rule | Value | Source |
|---|---|---|
| Gmail spam rate — hard ceiling | 0.30% | Google |
| Gmail spam rate — recommended target | 0.10% | Google |
| Gmail spam rate cadence | Calculated daily | Google, via Postmaster's `getComplianceStatus` `SPAM_RATE_HIGH` reason ([docs/postmaster-verdicts.md](docs/postmaster-verdicts.md)) |
| Gmail bulk-sender threshold | >5,000/day to Gmail, per primary domain | Google |
| Gmail enforcement (Nov 2025+) | SMTP `4.7.31`/`4.7.32` → escalating to `5.7.x` | Google, via Suped |
| Yahoo spam rate | <0.3%, on inbox-delivered mail | Yahoo Sender Hub |
| Microsoft high-volume threshold | ≥5,000/day to outlook/hotmail/live | Microsoft |
| Microsoft penalty | `550 5.7.515` — outright rejection, not junk-foldering | Microsoft, via dmarcian |
| SES bounce rate — review / pause | 5% / 10% | AWS |
| SES complaint rate — review / pause | 0.1% / 0.5% | AWS |

**Google's 0.30% and Amazon SES's 0.10% are NOT the same measurement, and this project never blends them into one number.** Google's denominator is Gmail inbox-delivered, DKIM-authenticated mail to engaged users. SES's is mail to domains that return complaint feedback to SES. The ladder above picks threshold values informed by both, as provider-agnostic policy points — never as a claim that the two rates are directly comparable. 0.30% is a *terminal* threshold: by the time evidence is strong enough to be confident it's been crossed, the damage is already done, because complaint data itself lags 24h–3 days behind send. 0.10% is the natural amber line instead — simultaneously Google's own recommended target and SES's own review trigger, two independent providers converging on the same number as "you should be worried now." See `engine/breaker.py`'s module docstring for the full rationale.

## What this cannot see

- **IP reputation.** For the dominant cold-outbound topology (Google Workspace / Microsoft 365 shared sending pools), IP reputation is both unobservable to a sender and not theirs to fix. Google itself removed the `DOMAIN_REPUTATION` and `IP_REPUTATION` metrics from Postmaster Tools v2 — there is no metric to read even if this project wanted one. This isn't a gap to be filled later; it's a design constraint, stated in `BUILD-PLAN.md`.
- **Inbox placement.** This tool has no visibility into whether a delivered message actually landed in the primary inbox versus a spam or promotions folder — it can see bounces, complaints, and compliance verdicts, not placement.
- **Postmaster's own confidence bounds — removed in v2.** Google's v1 Postmaster API exposed `userReportedSpamRatioLowerBound`/`UpperBound` alongside the point estimate. v2 dropped them entirely (confirmed against the live discovery document, [docs/postmaster-verdicts.md](docs/postmaster-verdicts.md)) — a real regression this project doesn't have a way to work around, only to be honest about: every Postmaster-sourced rate is treated as a bare point estimate, run through the same posterior machinery as any other rate.
- **Which campaign caused a spike, without the identity scheme adopted first.** Postmaster reports a domain-day scalar; the sequencer reports per-message events; there is no join key between them by default. `identity/feedback_id.py` and `identity/subdomain_advisor.py` fix this — but only for mail sent *after* they're adopted, and only if the underlying sending architecture actually follows the scheme. See [`docs/limits.md`](docs/limits.md).
- **Warmup adherence.** Not implemented in v0.1. When it lands, it will ship as explicitly-labeled vendor-consensus heuristics, not a standard — there is no RFC or independent research behind published warmup curves, and presenting folklore as authoritative would be this project's fastest way to lose credibility.

## License

[MIT](LICENSE)
