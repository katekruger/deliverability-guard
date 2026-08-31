---
status: "accepted"
date: "2026-08-30"
deciders: "Kate Kruger"
---

# Depend on parsedmarc for DMARC auth-health, rather than parsing aggregate XML directly

## Context and Problem Statement

`signals/dmarc.py` (BUILD-PLAN.md §4 item #17, v0.3) needs to turn DMARC
aggregate reports into an auth-health signal: what fraction of mail
claiming a domain is actually authenticating, and who's sending the rest.
DMARC aggregate reports (RUA) arrive as XML, frequently gzip- or
zip-compressed, sometimes as an email attachment fetched from an IMAP
mailbox rather than a file on disk. BUILD-PLAN.md itself already answers
the "should we write our own parser" question -- §1's gap analysis lists
`domainaware/parsedmarc` as prior art with the explicit instruction "Reuse
as a library. Do not reimplement." Should this project still hand-roll any
part of that, or depend on `parsedmarc` wholesale?

## Decision Drivers

- BUILD-PLAN.md's own research already concluded this shouldn't be
  reimplemented -- the question here is just confirming that conclusion
  and being honest about its cost, not re-litigating it.
- DMARC's aggregate-report XML schema is genuinely fiddly to parse
  correctly (namespace variations, `draft` vs. final schema versions,
  compressed attachments, IMAP retrieval) -- exactly the kind of format-
  parsing surface where a mature, widely-used library has already found
  and fixed the edge cases a from-scratch parser would rediscover slowly.
- Following `providers/ses.py`'s boto3 precedent (ADR 0005): a dependency
  addition needs its weight disclosed, not just its benefit.

## Decision Outcome

Chosen option: "depend on `parsedmarc`," for the same reason BUILD-PLAN.md
already gives -- this is a solved, actively-maintained parsing problem,
and this project's value-add is the aggregation policy on top (alignment
classification, unknown-source ranking, treating zero reports as
`INSUFFICIENT_DATA` rather than a rate), not re-solving XML parsing.

`signals/dmarc.py` takes `parsedmarc`'s own parsed-report dicts as input
(`summarize_auth_health(parsed_reports: Iterable[Mapping])`) rather than
calling `parsedmarc.parse_aggregate_report_xml` or
`parsedmarc.get_dmarc_reports_from_mailbox` internally -- this keeps the
IMAP/file/network retrieval question (which mailbox, which credentials,
how often to poll) entirely a caller decision, the same way
`engine.breaker.evaluate` takes `sends`/`complaints` as plain integers
rather than owning how a caller obtained them.

### Consequences

- Good, because this project doesn't own DMARC XML schema edge cases,
  compressed-attachment handling, or IMAP retrieval -- `parsedmarc` does,
  and it's exercised well beyond this project's own test suite.
- Good, because `tests/test_dmarc.py` includes a real integration test
  against `parsedmarc.parse_aggregate_report_xml(..., offline=True)` (a
  hand-authored sample report, `offline=True` to guarantee no reverse-DNS
  or GeoIP network lookup) -- this caught, before shipping, that
  `offline=True` parsing leaves `source.base_domain` as `None`, which is
  exactly why `_source_identifier`'s fallback to `source.ip_address`
  exists. Depending on the real library let this project verify its own
  assumption instead of merely asserting it, the way every hand-authored-
  fixture driver in this project (Instantly, Smartlead, lemlist, Apollo)
  has to just assert its shape assumptions are correct.
- Bad, because `parsedmarc` is a genuinely heavy dependency -- `uv sync`
  pulls in ~20 transitive packages (`lxml`, `cryptography`, `dnspython`,
  `imapclient`, `dkimpy`, `mailsuite`, and more) for a project that was
  `httpx`/`pyyaml`/`scipy` before this driver and the SES driver's `boto3`.
  Every contributor's install now pays this cost regardless of whether
  they touch DMARC at all.
- Bad, because `parsedmarc`'s public API surface (return dict shape) is
  not something this project controls -- a future `parsedmarc` release
  changing its output shape could silently break `_require_records`/
  `_record_is_aligned`/`_source_identifier`'s assumptions. The integration
  test mitigates this for the shape at time of writing, not forever.

### Confirmation

`tests/test_dmarc.py` combines fabricated-dict unit tests (covering every
malformed-input branch `MalformedDmarcReportError` can raise) with one
integration test against `parsedmarc`'s real parser in `offline=True`
mode -- no network call of any kind, confirmed deterministic and fast
enough to run on every CI invocation rather than being skipped as
"slow"/"integration-only."

## Assumption this relies on

That `parsedmarc`'s parsed-report dict shape (`records`, each with
`source`, `count`, `alignment`) is stable enough across the versions this
project's `pyproject.toml` range (`parsedmarc>=8.0`) allows to not need
re-verification on every dependency bump. The integration test in
`tests/test_dmarc.py` is exactly the mechanism that would catch a
violation of this assumption -- if a `parsedmarc` upgrade breaks it, that
test fails loudly rather than this module silently misclassifying reports.

## Known limitation

This module does not implement report *retrieval* -- no IMAP polling, no
file-system watching, no scheduled fetch. A caller must obtain parsed
reports themselves (via `parsedmarc.get_dmarc_reports_from_mailbox` or
equivalent) and hand them to `summarize_auth_health`. Wiring that
retrieval into the two-loop daemon (`loops/controller.py`) as a slow-loop
input is real, separately-scoped follow-up work, the same category of gap
ADR 0004 already documents for Postmaster/compliance signals.

## Pros and Cons of the Options

### Depend on parsedmarc (chosen)

- Good, because it matches BUILD-PLAN.md's own explicit instruction and
  avoids re-solving a genuinely fiddly parsing problem
- Bad, because of the dependency weight (~20 transitive packages) and the
  API-surface-not-owned risk noted above

### Hand-roll aggregate XML parsing

- Good, because it would keep the dependency footprint minimal
- Bad, because it directly contradicts BUILD-PLAN.md's own research
  conclusion, and would mean re-discovering schema edge cases (namespace
  variants, draft vs. final schema, compressed attachments) a mature
  library has already handled

## More Information

See `src/deliverability_guard/signals/dmarc.py` for the implementation,
`tests/test_dmarc.py` for the fabricated-dict and real-parser test suite,
and ADR 0005 for the parallel decision (and the same weight-vs-correctness
trade-off) for `providers/ses.py`'s `boto3` dependency.
