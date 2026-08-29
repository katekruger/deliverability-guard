# deliverability-guard — Build Plan

**A sending circuit breaker for B2B outbound email. Watches reputation, bounce, complaint and warmup signals across providers and automatically throttles or pauses sending before a domain burns.**

Owner: Kate Kruger (`github.com/katekruger`)
Status: not started
Plan version: 1.0 — 28 Aug 2026
Research current as of: 28 Aug 2026

---

## 0. Handover context — read this first

If you are a fresh session picking this up, five things will save you a week:

1. **Google removed domain reputation and IP reputation from Postmaster Tools.** Every "sender reputation monitor" built on those signals is now built on nothing. Postmaster v2 has no `DOMAIN_REPUTATION` or `IP_REPUTATION` metric. What replaced them — `getComplianceStatus`, a machine-readable verdict object — is arguably better for a circuit breaker, but the tool must be built on **rates and verdicts, not scores**.

2. **Naïve rate-threshold breakers are structurally useless at cold-outbound volume, and this is the intellectual core of the project.** 0.3% of 50 messages/day is 0.15 messages. The measurable outcomes are 0% or 2%. The metric is *quantized above the threshold you care about*. If you build a fixed-window percentage breaker you will have built Smartlead's Bounce Autopause with more steps. The correct answer is Bayesian posteriors with hierarchical pooling across mailboxes — see §6.

3. **Complaint data lags 24h–3 days.** A breaker reacting to a lagging indicator is a post-mortem generator. The architecture must be a **two-loop control system**: a fast loop on real-time leading indicators, and a slow loop that tunes the fast loop's thresholds. Treat the lag as known dead-time.

4. **The hardest problem is fixed at send time, not measure time.** Postmaster gives you a domain-day scalar; the sequencer gives you per-message events; there is no join key. The only bridges are per-campaign `Feedback-ID` headers and per-campaign-class subdomains. The tool must ship an opinionated identity scheme as a *prerequisite it helps users adopt*, or the correlation it promises is not computable.

5. **Nothing like this exists in open source.** I searched specifically. The closest prior art is Smartlead's proprietary, bounce-only, campaign-granularity "Bounce Autopause." The category is empty.

---

## 1. The gap

### What exists in open source

| Repo | Stars | License | What it does | Overlap |
|---|---|---|---|---|
| [`domainaware/parsedmarc`](https://github.com/domainaware/parsedmarc) | 1.3k | Apache-2.0 | DMARC aggregate + forensic + TLS-RPT (RFC 8460) parsing, many outputs | **Reuse as a library. Do not reimplement.** |
| [`happyDomain/happydeliver`](https://github.com/happyDomain/happydeliver) | 221 | AGPL-3.0 | Seed-email deliverability testing, A–F grading | Adjacent. Point-in-time test, not continuous. |
| [`monto-fe/smtp-probe`](https://github.com/monto-fe/smtp-probe) | 13 | MIT | Domain health 0–100, 29 RBLs, SMTP verification | Adjacent |
| [`gagandeep/email_deliverability`](https://github.com/gagandeep/email_deliverability) | 9 | MIT | Auth checks, FBL processing, IP warming schedules | Closest, but undocumented data sources |
| [`warmbly/warmbly`](https://github.com/warmbly/warmbly) | 1 | Apache-2.0 | Warmup ramps, per-mailbox caps | Abandoned at birth |

**What does not exist anywhere: a tool that reads reputation/complaint signals and automatically pauses an outbound sequencer.**

### The one proprietary competitor, and why it's weak

**Smartlead Bounce Autopause.** Formula: `(total_bounce_count + latest_bounce) / total_lead_count * 100`. Configurable threshold, no published default. Pauses the *campaign*. Emits a `campaign_bounce_threshold` webhook.

Three wedges:
1. **Bounce-only.** No complaint rate, no compliance verdict, no warmup adherence, no reply-rate ratio.
2. **Wrong denominator.** `total_lead_count`, not messages sent — it dilutes early in a campaign and is not comparable to Google's or SES's rate definitions.
3. **Single-vendor, campaign-granularity.** Cannot pause one bad mailbox, cannot see across domains, cannot act on Postmaster data.

---

## 2. Positioning

**One line:** a control loop for outbound sending that knows the difference between "no complaints" and "not enough data to say."

**Three defensible claims:**

1. **Statistically honest at cold-outbound volume.** It will refuse to trip on n=1, and it will say so, rather than either firing spuriously or silently reading missing data as good news.
2. **Cross-provider and per-mailbox.** Distribution beats totals — four mailboxes × 100 is safer than one × 400. So the breaker's unit is the mailbox, and it can trip one without halting a campaign.
3. **Throttles before it halts.** Smartlead's daily-limit endpoint and SES's config-set controls mean graduated response is possible. A kill switch is a worse product than a dimmer.

**What it is NOT:** a warmup service, an inbox-placement tester, an ESP, or a replacement for `parsedmarc`.

---

## 3. Scope

### v0.1 — the honest single-provider breaker (target: 2.5 weeks)

| In | Out |
|---|---|
| Instantly provider driver (read + per-mailbox pause) | Every other provider |
| Fast loop: bounce webhooks, delivery errors, account disconnects | Slow loop |
| Bayesian posterior complaint/bounce estimation | Postmaster integration |
| `OK` / `INSUFFICIENT_DATA` / `STALE` as first-class states | Warmup adherence |
| Dry-run mode (alert only, never act) — **the default** | Multi-tenant |
| Config as YAML, state in SQLite | A UI |

### v0.2 — the slow loop (target: +2 weeks)

- Google Postmaster Tools v2: `SPAM_RATE`, `DELIVERY_ERROR_RATE`, `AUTH_SUCCESS_RATE`, `FEEDBACK_LOOP_SPAM_RATE`
- `getComplianceStatus` verdicts as a hard gate
- Slow loop tuning fast-loop thresholds
- Hierarchical pooling across mailboxes on a domain
- Smartlead driver (throttle via daily-limit endpoint)

### v0.3 — identity scheme + breadth (target: +3 weeks)

- `Feedback-ID` header scheme generator and validator — the attribution unlock
- Subdomain segregation advisor
- `parsedmarc` integration for auth-health signal
- SES, Lemlist, Apollo drivers
- Warmup curve adherence, shipped as explicitly-labeled heuristics

### Explicit non-goals, permanently

- Sending email. This tool never sends.
- Warming mailboxes. It observes warmup; it does not perform it.
- Claiming inbox placement. It cannot see the inbox.
- IP-level reputation. For the dominant cold-outbound topology (Workspace/M365 shared pools) it is both unobservable and not yours to fix. State this as a design constraint, not a limitation.

---

## 4. Feature inventory, scoped

| # | Feature | Fills what gap | Effort | Verdict |
|---|---|---|---|---|
| 1 | Provider driver interface with **capability declaration** (`read_stats`/`throttle`/`pause`, each optional) | Two of nine providers can't pause at all | 3d | **v0.1** |
| 2 | Instantly driver (per-mailbox daily stats + per-mailbox pause) | Only vendor with both primitives | 3d | **v0.1** |
| 3 | Bayesian (beta-binomial) posterior on complaint/bounce rate | The whole statistical honesty story | 4d | **v0.1** |
| 4 | Data-availability state machine: `OK`/`INSUFFICIENT_DATA`/`STALE` | Absence of data ≠ good performance | 2d | **v0.1** |
| 5 | Fast loop: bounce webhooks, SMTP 4.7.x/5.7.x, account disconnect | The only real-time signal | 3d | **v0.1** |
| 6 | Dry-run as default, with a diff of what it *would* have done | Trust before autonomy — on-thesis | 1d | **v0.1** |
| 7 | Graduated response ladder (warn → throttle → pause) | Better than a kill switch | 2d | **v0.1** |
| 8 | Decision log: every evaluation, inputs, verdict, action | Auditability; also the `agent-audit` tie-in | 2d | **v0.1** |
| 9 | Google Postmaster v2 client | The best complaint signal available | 3d | v0.2 |
| 10 | `getComplianceStatus` verdicts as a hard gate | Google telling you directly that you're non-compliant | 2d | v0.2 |
| 11 | Two-loop controller (slow loop tunes fast loop) | Handles the 24–72h dead time | 4d | v0.2 |
| 12 | Hierarchical pooling across mailboxes on a domain | The mathematically correct fix for low volume | 3d | v0.2 |
| 13 | Smartlead driver (throttle via daily-limit) | Second provider; proves the throttle path | 2d | v0.2 |
| 14 | Sequential change detection (CUSUM/SPRT) | Catches trend shifts fixed windows miss | 3d | v0.2 |
| 15 | `Feedback-ID` scheme generator + validator | **The attribution unlock nobody uses** | 3d | v0.3 |
| 16 | Subdomain segregation advisor | Makes Postmaster's per-domain aggregation *become* attribution | 2d | v0.3 |
| 17 | `parsedmarc` integration (auth health, unknown-source detection) | Cross-provider signal, free | 2d | v0.3 |
| 18 | SES driver (CloudWatch + config-set sending controls) | Only platform with native breaker primitives | 3d | v0.3 |
| 19 | Warmup adherence, per-mailbox | Vendor folklore, labeled as such | 3d | v0.3 |
| 20 | Lemlist driver (idempotent pause) | Cheap; pause is documented idempotent | 1d | v0.3 |
| 21 | Apollo driver (`/abort`) | Cheap, but verify resume semantics | 1.5d | v0.3 |
| 22 | Outreach driver | Best-in-class webhooks; pause endpoint unverified | 3d | Deferred |
| 23 | Salesloft driver | Cadence pause may be **UI-only** — likely alert-only | 2d | Deferred |
| 24 | Amplemarket driver | No status-change API; alert-only at best | 1d | Deferred |
| 25 | Spamhaus DQS blocklist checks | Only free programmatic blocklist worth having | 2d | v0.3 |
| 26 | Web dashboard | Nice, not the product | 5d | Deferred |
| 27 | MCP server wrapping the read surface | Ties into the rest of the portfolio | 2d | v0.3 |

---

## 5. Architecture — the two-loop controller

```
FAST LOOP (seconds–minutes, real-time, leading indicators)
  ├─ bounce webhooks (Instantly, Smartlead, Outreach, SES/SNS)
  ├─ SMTP response codes: 4.7.31, 4.7.32, 5.7.515, 5.7.x
  ├─ mailbox disconnect events (Smartlead EMAIL_ACCOUNT_DISCONNECTED)
  └─ TLS delivery failures
        │
        ▼
   ┌─────────────────┐        thresholds
   │  BREAKER ENGINE │ ◀──────────────────┐
   └─────────────────┘                    │
        │                                 │
        ▼                          SLOW LOOP (daily, lagging, tunes fast loop)
   ACTION LADDER                     ├─ Postmaster v2 SPAM_RATE
   warn → throttle → pause           ├─ Postmaster getComplianceStatus verdicts
        │                            ├─ Postmaster FEEDBACK_LOOP_SPAM_RATE (per campaign)
        ▼                            ├─ FBL/ARF inbox (Yahoo CFL, Microsoft JMRP)
   PROVIDER DRIVER                   ├─ parsedmarc RUA (auth health)
   (capability-gated)                └─ Spamhaus DQS
```

**Why two loops.** Complaints surface 24h–3 days after send. For a 50/day mailbox that's 150 messages already gone; for a 40-mailbox farm, 6,000. The fast loop must act on what is observable now. The slow loop's job is not to trip the breaker — it is to say "your current fast-loop thresholds were too loose for the last three days" and tighten them.

### Provider driver interface

```python
class ProviderDriver(Protocol):
    capabilities: frozenset[Capability]   # READ_STATS | THROTTLE | PAUSE | WEBHOOKS

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]: ...
    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult: ...
    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult: ...
```

**The engine must degrade gracefully to alert-only when a verb is unsupported.** Do not design an interface that assumes every provider can pause.

### Provider capability matrix (verified)

| Provider | Read stats | Pause campaign | Pause/throttle mailbox | Bounce webhook |
|---|:--:|:--:|:--:|:--:|
| **Instantly** | per-mailbox daily | yes | **pause** | yes |
| **Smartlead** | per-campaign | yes | **throttle** (daily limit) | yes |
| **Lemlist** | activities | yes, idempotent | no | unverified |
| **Apollo** | email stats | yes (`/abort`) | list only | polling |
| **Outreach** | yes | likely | likely | best-in-class |
| **Salesloft** | yes | **likely UI-only** | no | unverified |
| **Amplemarket** | no | **app-only** | no | no |
| **SES** | CloudWatch | config set | account | SNS |
| **Postmark** | yes | no | no | yes |
| **SendGrid** | yes | no | no | yes |

**Build the reference integration on Instantly.** It is the only vendor exposing both per-mailbox daily bounce data and per-mailbox pause — exactly the circuit breaker's minimum viable substrate.

### Key endpoints (verified)

**Instantly** — `https://api.instantly.ai`, Bearer, scoped
```
POST /api/v2/accounts/{email}/pause          # scopes: accounts:update | accounts:all | all:update | all:all
POST /api/v2/campaigns/{id}/pause
POST /api/v2/campaigns/{id}/activate
GET  /api/v2/accounts/analytics/daily        # sent, bounced — per mailbox per day
POST /api/v2/accounts/warmup-analytics       # sent, landed_inbox, landed_spam, health_score
GET  /api/v2/campaigns/analytics/daily
```

**Smartlead** — `https://server.smartlead.ai/api/v1`, `?api_key=` query param
```
PATCH /campaigns/{id}/status                 # START | PAUSED | STOPPED
POST  /email-accounts/{id}                   # update daily limit ← THE THROTTLE PRIMITIVE
GET   /campaigns/{id}/statistics
```
Note: API-key-in-query-string lands in logs and referrers. Put it in the threat model.

**Google Postmaster v2** — `https://gmailpostmastertools.googleapis.com`, OAuth
```
POST /v2/{parent=domains/*}/domainStats:query      # pageSize max 200; DAILY | OVERALL
POST /v2/domainStats:batchQuery
GET  /v2/{name=domains/*/complianceStatus}
     /v2/domains — create, verify, getVerificationToken   ← programmatic onboarding
```
Metrics: `SPAM_RATE`, `FEEDBACK_LOOP_ID`, `FEEDBACK_LOOP_SPAM_RATE`, `AUTH_SUCCESS_RATE`, `TLS_ENCRYPTION_RATE`, `DELIVERY_ERROR_RATE`, `DELIVERY_ERROR_COUNT`. Scopes: `.../auth/postmaster` or `.../auth/postmaster.traffic.readonly`.

---

## 6. The statistics — specified, because this is the differentiator

### The problem, concretely

A sender at 50/day. Gmail's ceiling is 0.3%. 0.3% of 50 is **0.15 messages**. Observable outcomes: 0 complaints (0%) or 1 complaint (**2%**, 6.7× the limit). There is nothing in between. Getting into a regime where 0.3% is even representable needs ~333 messages to that provider — weeks of accumulation, by which point the data is stale.

### The fix

**1. Beta-binomial posterior, not a point estimate.**
Model complaints as `Beta(α₀ + complaints, β₀ + sends − complaints)`. Trip on the **lower bound of the credible interval** crossing the threshold, not the point estimate. This naturally refuses to fire on n=1: one complaint in 50 sends gives a posterior whose 5th percentile is nowhere near 0.3%.

Prior: use a weakly informative `Beta(0.5, 500)` — centred near 0.1%, easily overwhelmed by real data. Make it configurable and document why it exists.

**2. Hierarchical pooling.**
A single mailbox never has enough data; forty mailboxes on one domain might. Partial-pooling across mailboxes within a domain, and across domains within a tenant, is the mathematically correct answer to low volume. A mailbox with 50 sends inherits the domain's prior; a mailbox with 5,000 sends dominates its own posterior.

**3. Sequential change detection for trends.**
CUSUM or SPRT on the bounce/complaint stream catches "something changed on Tuesday" faster than any fixed-window rate. Fixed windows are the wrong tool for a monitoring problem with a known dead time.

**4. Check whether Postmaster v2 retains confidence bounds.**
v1 had `userReportedSpamRatioLowerBound` / `UpperBound` — Google knew this was a problem. **Verify whether v2's `SPAM_RATE` keeps them.** If yes, use them directly. If no, that is a meaningful regression worth documenting publicly.

### The README must say this

> For senders under roughly 1,000 messages/day/provider, this tool is a leading-indicator and compliance monitor, not a statistically valid complaint-rate breaker. Overclaiming here would make it wrong in production.

That sentence is a credibility asset. Ship it.

---

## 7. Thresholds — exact, sourced, and why the defaults are lower

| Rule | Value | Source |
|---|---|---|
| Gmail bulk sender threshold | >5,000/day to Gmail, per **primary domain** (subdomains aggregate) | Google |
| Gmail classification | Once bulk, **does not expire** | Google FAQ |
| Gmail spam rate — hard ceiling | **0.30%** | Google |
| Gmail spam rate — target | **0.10%** | Google |
| Gmail spam rate cadence | Calculated **daily** | Google FAQ |
| Gmail DMARC | Required for bulk; `p=none` sufficient | Google |
| Honor unsubscribe within | **2 days** | Google + Yahoo |
| Gmail enforcement (from Nov 2025) | `4.7.31` (no DMARC), `4.7.32` (From not aligned) → escalating to `5.7.x` | Suped |
| Yahoo spam rate | **<0.3%**, on inbox-delivered mail | Yahoo Sender Hub |
| Yahoo volume threshold | **Deliberately unpublished** | Yahoo FAQ |
| Microsoft threshold | ≥**5,000/day** to outlook/hotmail/live | Microsoft |
| Microsoft penalty | `550 5.7.515 Access denied` — **rejection**, not junk (Apr 29 2025 update) | dmarcian |
| SES bounce → review / pause | **5%** / **10%** | AWS |
| SES complaint → review / pause | **0.1%** / **0.5%** | AWS |

### Recommended default ladder

| State | Complaint rate (posterior lower bound) | Action |
|---|---|---|
| Warn | ≥ 0.05% | Notify, no action |
| Throttle | ≥ 0.10% | Reduce daily limit 50% |
| Pause | ≥ 0.20% | Pause mailbox |

**Rationale:** 0.3% is a *terminal* threshold — by the time you measure it, the damage is done, because complaints surface days late. 0.1% is the natural amber line: it is simultaneously Google's recommended target and SES's own review trigger.

**Caveat that belongs in the README:** Google's 0.3% and SES's 0.1% are **not the same measurement**. Google's denominator is Gmail inbox-delivered, DKIM-authenticated mail to engaged users. SES's is mail to domains that return complaint feedback to SES. They must never be blended into one number.

---

## 8. Known landmines

| Landmine | Detail | Mitigation |
|---|---|---|
| **Postmaster privacy threshold** | Google omits low-volume days entirely. Threshold **unpublished** (community estimates ~50–100/day, unofficial). | Model as `INSUFFICIENT_DATA`, never coerce missing → zero. **Treat a transition from having data to not having data as its own alert** — a throttled domain drops below the threshold and monitoring goes dark exactly when things are worst. |
| **Google removed domain/IP reputation** | No `DOMAIN_REPUTATION` or `IP_REPUTATION` in v2. Confirmed by Google's own deprecation page. | Build on rates + `getComplianceStatus` verdicts. |
| **Postmaster v1 status is contested** | Google says retirement is coming with **no date**; vendor blogs claim it already shut down end-2025. | Build against v2. Make "does v1 still answer?" a CI probe. |
| **SNDS moved and broke** | June 8 2026 portal migration; June 22 2026 the `sndsApi.aspx` automated URLs were deprecated. **Any existing SNDS scraper is dead.** New REST API with OAuth. Report links **expire after 30 days**. Trap hit counts **removed entirely** July 22 2026. JMRP now header-only ARF with sender address redacted. Reattestation every 10 months. | Budget real engineering time. **Sources disagree on the new URL** — verify empirically. Deprioritize to v0.3+. |
| **Yahoo has no pull API** | CFL delivers ARF **by email** only. DKIM-signed mail only. | Requires an inbox + ARF parser. Treat as v0.3. |
| **Talos / SenderScore / Validity** | No public APIs. Cisco employee on record confirming this. | Skip. Spamhaus DQS is the only free programmatic blocklist worth having. |
| **DMARC RFCs changed** | **RFC 9989 (May 2026) obsoletes 7489.** Aggregate reporting → RFC 9990, failure → RFC 9991. The `ri` tag is **removed** — you can no longer request shorter intervals; receivers SHOULD send at least every 24h. `pct` removed, replaced by binary `t`. New `np`/`psd` tags. PSL replaced by DNS tree-walk. | Cite 9989/9990/9991, not 7489. RUA is a **slow-loop auth-health signal only** — it has no complaint or placement data. |
| **Shared IP topology** | Most cold outbound runs on Workspace/M365 shared pools you neither control nor can measure. IP reputation isn't yours to fix and Postmaster no longer exposes it. | The unit of reputation is the **sending domain**. Levers are volume, content, list quality — never IP. State as a design constraint. |
| **No join key between systems** | Postmaster gives a domain-day scalar; the sequencer gives per-message events. You cannot attribute "which campaign caused Tuesday's spike." | Ship the identity scheme (§9) as a prerequisite. This is the most important product insight in the plan. |
| **Instantly rate limits undocumented** | Not published. | Assume 429s, exponential backoff, measure empirically. |
| **Warmup curves are folklore** | No RFC, no M3AAWG document, no independent research. Every source with numbers is a warmup vendor selling warmup. | Ship curves as **explicitly-labeled, user-overridable heuristics** citing "vendor consensus, not standard." Presenting folklore as authoritative is the project's most likely credibility failure. |

---

## 9. The identity scheme — the underrated feature

Postmaster v2 exposes `FEEDBACK_LOOP_ID` and `FEEDBACK_LOOP_SPAM_RATE`. If you set a `Feedback-ID:` header per campaign, **Postmaster reports spam rate per campaign.** This is the single best attribution mechanism available in email and it is badly underused.

`deliverability-guard` should ship:

1. **A `Feedback-ID` scheme generator** — a documented convention (`campaign:segment:mailbox:tenant`) plus a validator that checks outgoing mail actually carries it.
2. **A subdomain segregation advisor** — send each campaign class from a distinct subdomain so Postmaster's per-domain aggregation *becomes* the attribution. This is an operational requirement imposed on users, not a code feature, and the tool should be honest about that.
3. **VERP return-path guidance** — required for Microsoft JMRP attribution now that the body and sender address are stripped.

Make this a headline feature. It is the difference between a tool that reports numbers and a tool that tells you which campaign to stop.

---

## 10. Repo structure

```
deliverability-guard/
├── README.md                    # the honest-limits section goes ABOVE the feature list
├── LICENSE                      # MIT
├── CHANGELOG.md · CONTRIBUTING.md · SECURITY.md
├── pyproject.toml
├── .github/workflows/{ci,codeql,v1-probe}.yml
├── src/deliverability_guard/
│   ├── engine/
│   │   ├── breaker.py           # the ladder
│   │   ├── posterior.py         # beta-binomial + hierarchical pooling
│   │   ├── changepoint.py       # CUSUM/SPRT
│   │   └── state.py             # OK | INSUFFICIENT_DATA | STALE
│   ├── loops/{fast.py,slow.py}
│   ├── providers/
│   │   ├── base.py              # Protocol + Capability enum
│   │   ├── instantly.py · smartlead.py · lemlist.py · apollo.py · ses.py
│   ├── signals/
│   │   ├── postmaster.py · fbl_arf.py · spamhaus.py · dmarc.py  # wraps parsedmarc
│   ├── identity/
│   │   ├── feedback_id.py · subdomain_advisor.py
│   ├── audit/log.py             # decision log — every evaluation, inputs, verdict, action
│   └── cli.py
├── config/thresholds.example.yml
├── docs/
│   ├── statistics.md            # why fixed-window rates are wrong — the flagship doc
│   ├── limits.md                # what this tool cannot see
│   ├── identity-scheme.md
│   └── providers.md             # the capability matrix
└── tests/
    ├── fixtures/                # synthetic send/complaint streams at 50, 500, 5000/day
    └── test_posterior_refuses_n1.py   # the test that proves the thesis
```

---

## 11. Milestones

| # | Deliverable | Done when |
|---|---|---|
| M1 | Driver interface + Instantly driver | Reads per-mailbox daily stats; `pause` works against a sandbox account |
| M2 | Posterior engine | `test_posterior_refuses_n1` passes; 1-in-50 does not trip, 40-in-5000 does |
| M3 | State machine | Missing Postmaster data yields `INSUFFICIENT_DATA`, never `OK` |
| M4 | Fast loop + ladder | Webhook → evaluation → throttle, end to end, in dry-run |
| M5 | Decision log | Every evaluation reproducible from the log alone |
| M6 | **v0.1.0 + terminal GIF** | Released; GIF shows a breaker *declining* to trip on n=1 and then tripping on real signal |
| M7 | Postmaster v2 client + verdicts | Slow loop reads live data |
| M8 | Hierarchical pooling | A 50/day mailbox inherits its domain's posterior |
| M9 | `Feedback-ID` scheme + validator | Per-campaign spam rate readable from Postmaster |

---

## 12. Distribution

1. **awesome-mcp-servers** (Marketing category, sparse) once the MCP wrapper ships.
2. **PyPI**, with a trusted publisher.
3. **`awesome-gtm-engineering`** — your own list.
4. **Hacker News.** The statistics angle is the story, not the tool: *"Your cold email bounce monitor is measuring 0.15 of a message."* HN has an active, opinionated deliverability practitioner cohort — they described hand-rolling exactly this.
5. **r/coldemail, r/Emailmarketing** — but lead with the honest-limits section, not the feature list. That audience punishes overclaiming.
6. **M3AAWG** — a genuinely rigorous open-source deliverability tool is the kind of thing that community notices.

---

## 13. Open questions to resolve before M1

1. **Pull the Postmaster v2 discovery doc** — `https://gmailpostmastertools.googleapis.com/$discovery/rest?version=v2` — and enumerate `DeliverabilityStatusVerdict`, `OneClickUnsubscribeVerdict`, `HonorUnsubscribeVerdict` values and reason codes. **Highest priority; the state machine depends on it.**
2. **Does v2 `SPAM_RATE` retain confidence bounds?** v1 had them. Materially changes §6.
3. **Is Postmaster v1 actually still serving?** Probe it; sources conflict.
4. **SNDS**: confirm the real new portal URL (Postmastery and Suped disagree) and get the REST API spec + OAuth flow. No official Microsoft doc was locatable.
5. **SES API action names** for account and config-set sending enable/disable, v1 and v2. Likely `UpdateAccountSendingEnabled` / `PutAccountSendingAttributes` but **unverified**.
6. **Outreach**: confirm sequence-pause and mailbox-disable endpoints exist.
7. **Salesloft**: confirm whether cadence pause is API-reachable at all. If UI-only, ship as alert-only.
8. **Measure Instantly's rate limits** empirically.

---

## 14. Sources

- [Gmail Postmaster Tools API](https://developers.google.com/workspace/gmail/postmaster) · [v2 migration](https://developers.google.com/workspace/gmail/postmaster/guides/migration-v2) · [v2 domainStats.query](https://developers.google.com/workspace/gmail/postmaster/reference/rest/v2/domains.domainStats/query) · [v2 getComplianceStatus](https://developers.google.com/workspace/gmail/postmaster/reference/rest/v2/domains/getComplianceStatus) · [v1 deprecation notice](https://support.google.com/a/answer/16594218) · [Google sender guidelines](https://support.google.com/a/answer/81126)
- [Microsoft high-volume sender requirements](https://techcommunity.microsoft.com/blog/microsoftdefenderforoffice365blog/strengthening-email-ecosystem-outlook%e2%80%99s-new-requirements-for-high%e2%80%90volume-senders/4399730) · [dmarcian: Microsoft enforcement](https://dmarcian.com/microsoft-enforces-spf-dkim-dmarc/) · [Postmastery: SNDS 2026](https://www.postmastery.com/the-first-big-snds-update-in-years-what-changed-and-why-it-matters/) · [Suped: SNDS 2026](https://www.suped.com/blog/microsoft-snds-2026-changes-what-senders-need-to-know-before-june-8)
- [Yahoo Sender Hub CFL](https://senders.yahooinc.com/complaint-feedback-loop/) · [Yahoo best practices](https://senders.yahooinc.com/best-practices/)
- [RFC 9989 (DMARC)](https://www.rfc-editor.org/rfc/rfc9989.txt) · [dmarcian: DMARC RFC updates](https://dmarcian.com/dmarc-rfc-updates/) · [Spamhaus free DQS](https://www.spamhaus.com/data-access/free-data-query-service/)
- [Instantly pause account](https://developer.instantly.ai/api/v2/account/pauseaccount) · [Instantly analytics](https://developer.instantly.ai/api/v2/analytics) · [Smartlead API](https://helpcenter.smartlead.ai/en/articles/125-full-api-documentation) · [Smartlead bounce autopause](https://helpcenter.smartlead.ai/en/articles/210-bounce-autopause-and-webhook) · [lemlist pause](https://developer.lemlist.com/api-reference/endpoints/campaigns/pause-campaign) · [Apollo deactivate sequence](https://docs.apollo.io/reference/deactivate-sequence) · [Outreach webhooks](https://developers.outreach.io/api/webhooks)
- [AWS SES enforcement FAQ](https://docs.aws.amazon.com/ses/latest/dg/faqs-enforcement.html) · [SES automatic pausing](https://docs.aws.amazon.com/ses/latest/DeveloperGuide/monitoring-sender-reputation-pausing.html) · [Postmark Bounce API](https://postmarkapp.com/developer/api/bounce-api) · [SendGrid Event Webhook](https://www.twilio.com/docs/sendgrid/for-developers/tracking-events/event)
- [parsedmarc](https://github.com/domainaware/parsedmarc) · [happydeliver](https://github.com/happyDomain/happydeliver) · [smtp-probe](https://github.com/monto-fe/smtp-probe) · [email_deliverability](https://github.com/gagandeep/email_deliverability) · [warmbly](https://github.com/warmbly/warmbly)
