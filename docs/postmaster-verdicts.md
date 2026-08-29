# Postmaster Tools v2: verdict enums, reason codes, and the confidence-bounds regression

BUILD-PLAN.md §13 flagged two open questions blocking the design of
`signals/postmaster.py` and its state machine. This document resolves both,
sourced directly from the live discovery document:

```
https://gmailpostmastertools.googleapis.com/$discovery/rest?version=v2
```

fetched 2026-08-29. The discovery document is generated from Google's own
API definitions, so this is the ground truth, not a secondhand description
— quotes below are copied verbatim from its `enumDescriptions`.

## 1. The three verdict enums

All three verdicts share a common shape: a `status` (or `state`) field of
type `ComplianceStatus`, and a `reason` field explaining *why*, populated
only when the status isn't compliant.

### `ComplianceStatus` (shared by all three verdicts)

| Value | Meaning |
|---|---|
| `STATE_UNSPECIFIED` | Unspecified. |
| `COMPLIANT` | The compliance requirement is met, and the sender is deemed compliant. |
| `NEEDS_WORK` | The compliance requirement is unmet, and the sender needs to do work to achieve compliance. |

### `DeliverabilityStatusVerdict.reason`

> "[Developer Preview](https://developers.google.com/workspace/preview)" —
> this whole type is explicitly marked preview/unstable by Google itself.
> `signals/postmaster.py` treats it accordingly: read it, never treat its
> absence as an error.

| Value | Meaning |
|---|---|
| `REASON_UNSPECIFIED` | Unspecified. |
| `MESSAGE_VOLUME_LOW` | Not enough outgoing email. |
| `SMTP_ERRORS_HIGH` | Many messages with delivery errors. |
| `SENDER_NOT_COMPLIANT` | The sender does not meet the sender requirements. |
| `SPAM_RATE_HIGH` | The spam rate is above 0.1%. |
| `USER_FEEDBACK_NEGATIVE` | Indicates users do not want to receive email messages. |
| `USER_FEEDBACK_LOW` | Users do not take action on messages. |
| `USER_FEEDBACK_POSITIVE` | Users signal they want to receive email messages. |

Two things worth calling out:

- `MESSAGE_VOLUME_LOW` is Google's own admission that low volume is a
  distinct, named condition worth a verdict of its own — independent
  confirmation of this project's entire low-volume thesis, from the source
  that matters most for Gmail deliverability.
- `SPAM_RATE_HIGH`'s description states the threshold explicitly: **above
  0.1%**, not 0.3%. That's the same 0.1% this project's default `throttle`
  rung and Google's own recommended target both already converge on
  (BUILD-PLAN.md §7) — now confirmed as the number that drives Google's own
  compliance verdict too, not just a target Google recommends separately
  from enforcement.

### `OneClickUnsubscribeVerdict.reason`

| Value | Meaning |
|---|---|
| `REASON_UNSPECIFIED` | Unspecified. |
| `NO_UNSUB_GENERAL` | Sender does not support one-click unsubscribe for the majority of their messages. |
| `NO_UNSUB_SPAM_REPORTS` | Sender does not support one-click unsubscribe for most messages that are manually reported as spam. |
| `NO_UNSUB_PROMO_SPAM_REPORTS` | Sender does not support one-click unsubscribe for most promotional messages that are manually reported as spam. This classification of messages is a subset of those encompassed by `NO_UNSUB_SPAM_REPORTS`. |

### `HonorUnsubscribeVerdict.reason`

| Value | Meaning |
|---|---|
| `REASON_UNSPECIFIED` | Unspecified. |
| `NOT_HONORING` | The sender does not honor unsubscribe requests. |
| `NOT_HONORING_TOO_FEW_CAMPAIGNS` | The sender does not honor unsubscribe requests and consider to increase the number of relevant campaigns. |
| `NOT_HONORING_TOO_MANY_CAMPAIGNS` | The sender does not honor unsubscribe requests and consider to reduce the number of relevant campaigns. |

(The awkward grammar in the last two descriptions — "consider to increase"
— is copied verbatim from Google's own discovery document, not a transcription
error on our part.)

### How the three compose: `DomainComplianceData`

`getComplianceStatus` returns a `DomainComplianceStatus`, whose
`complianceData` (and, for a subdomain, `subdomainComplianceData`) is a
`DomainComplianceData` bundling all three verdicts plus a `rowData` array —
one `ComplianceRowData` per underlying requirement, each with its own
`ComplianceStatus`:

```
COMPLIANCE_REQUIREMENT_UNSPECIFIED, SPF, DKIM, SPF_AND_DKIM, DMARC_POLICY,
DMARC_ALIGNMENT, MESSAGE_FORMATTING, DNS_RECORDS, ENCRYPTION,
USER_REPORTED_SPAM_RATE, ONE_CLICK_UNSUBSCRIBE, HONOR_UNSUBSCRIBE
```

`identity/`'s hard-gate wiring (see `signals/postmaster.py`) treats
`deliverabilityStatusVerdict.status == NEEDS_WORK` as sufficient on its own
to trip regardless of volume — Google telling you directly that you're
non-compliant outranks any statistical inference (BUILD-PLAN.md §5). The
`rowData` breakdown is surfaced for humans reading the decision log, not
consulted by the automatic gate itself, since the aggregate verdict is
already the actionable signal and drilling into which sub-requirement
failed is diagnostic information, not a different action to take.

## 2. Does v2 `SPAM_RATE` retain confidence bounds?

**No. This is a confirmed regression from v1, not a documentation gap.**

v1's traffic stats included `userReportedSpamRatioLowerBound` and
`userReportedSpamRatioUpperBound` alongside the point estimate —
independent evidence that Google itself recognized the low-volume
uncertainty problem this entire project exists to solve.

v2 replaced that shape entirely. A metric's value in v2 comes back as a
`StatisticValue`, whose complete set of fields is:

```
doubleValue, floatValue, intValue, stringValue, stringList
```

There is no lower-bound or upper-bound field anywhere in that type, nor
anywhere else in the v2 discovery document. `SPAM_RATE` (and every other
`BaseMetric.standardMetric`) comes back as a bare point estimate, full stop.

**Consequence for this project:** `signals/postmaster.py` cannot delegate
uncertainty quantification to Google in v2 the way a client could have
partially leaned on v1's bounds. Every Postmaster-sourced rate this project
consumes gets fed into `engine/posterior.py`'s beta-binomial machinery like
any other rate — Postmaster's own point estimate is treated as no more
inherently trustworthy at low volume than a raw `complaints / sends`
calculation would be, because as far as v2 is concerned, structurally, it
*is* one.

This should be stated publicly (`docs/statistics.md` and/or the eventual
README limits section, Prompt 5): Google had a better answer to this exact
problem in v1 and removed it in v2. That's worth saying plainly, not
softening — it strengthens this project's argument rather than undermining
it: the tool exists precisely because the platforms it depends on don't
solve this for you, and in at least one documented case, used to solve it
better than they do now.

## 3. Metrics confirmed (`BaseMetric.standardMetric`)

```
STANDARD_METRIC_UNSPECIFIED, FEEDBACK_LOOP_ID, FEEDBACK_LOOP_SPAM_RATE,
SPAM_RATE, AUTH_SUCCESS_RATE, TLS_ENCRYPTION_MESSAGE_COUNT,
TLS_ENCRYPTION_RATE, DELIVERY_ERROR_COUNT, DELIVERY_ERROR_RATE
```

Matches BUILD-PLAN.md §5's list exactly, with one addition
(`TLS_ENCRYPTION_MESSAGE_COUNT`, a count alongside the rate). **Confirmed:
no `DOMAIN_REPUTATION` or `IP_REPUTATION` metric exists anywhere in the v2
schema.** `signals/postmaster.py` does not look for one.

## 4. Endpoints confirmed (method, HTTP verb, path)

| Method | HTTP | Path |
|---|---|---|
| `domainStats.batchQuery` | POST | `v2/domainStats:batchQuery` |
| `domains.domainStats.query` | POST | `v2/{+parent}/domainStats:query` |
| `domains.create` | POST | `v2/domains` |
| `domains.get` | GET | `v2/{+name}` |
| `domains.list` | GET | `v2/domains` |
| `domains.delete` | DELETE | `v2/{+name}` |
| `domains.verify` | POST | `v2/{+name}:verify` |
| `domains.getVerificationToken` | GET | `v2/{+name}` |
| `domains.getComplianceStatus` | GET | `v2/{+name}` |

`QueryDomainStatsRequest.pageSize`'s description confirms the max is 200,
default 10, matching BUILD-PLAN.md §5. `aggregationGranularity` is
`DAILY` (default) or `OVERALL`. `TimeQuery` takes either a `DateRanges` list
or a `DateList` of specific dates — not a single fixed range shape.

`DomainStat.date` is documented as populated "if granularity is DAILY" —
i.e. **rows are keyed by whichever dates the server chooses to return, not
by a fixed calendar the client controls.** This is the schema-level
confirmation of BUILD-PLAN.md §8's "gaps are normal, not errors" landmine:
there is no field anywhere in this response shape that represents "zero
activity on a missing day," because a missing day simply doesn't produce a
`DomainStat` at all.

## 5. Auth scopes confirmed

```
https://www.googleapis.com/auth/postmaster                    (full)
https://www.googleapis.com/auth/postmaster.user
https://www.googleapis.com/auth/postmaster.domain
https://www.googleapis.com/auth/postmaster.traffic.readonly
```

`signals/postmaster.py` should request the narrowest scope that covers what
it actually does — `postmaster.traffic.readonly` plus `postmaster.domain`
for the create/verify flow, not the blanket `postmaster` scope, once OAuth
wiring is implemented (out of scope for this PR, which uses recorded
fixtures only, consistent with `AGENTS.md`'s no-live-calls-in-tests rule).

## 6. What this resolves in BUILD-PLAN.md §13

- **Item 1 (verdict enums and reason codes): resolved above, verbatim from source.**
- **Item 2 (does v2 retain confidence bounds): resolved above — no, it does not.**

Items 3-8 in that section (Postmaster v1 serving status, SNDS, SES action
names, Outreach/Salesloft endpoint confirmation) are out of scope for this
prompt and remain open.
