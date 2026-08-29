# Postmaster fixtures — provenance

Different from `tests/fixtures/{instantly,smartlead}/README.md`'s caveat:
these fixtures' **shapes are verified against the real, live Postmaster
Tools v2 discovery document**
(`https://gmailpostmastertools.googleapis.com/$discovery/rest?version=v2`,
fetched 2026-08-29 -- see `docs/postmaster-verdicts.md` for the full
enumeration this was built from). Every field name, every enum value, and
every nesting level here matches that document exactly.

What's still illustrative: the actual **values** (which domain, which
metrics, which dates, whether a given verdict is `COMPLIANT` or
`NEEDS_WORK`) — this environment has no live, verified Postmaster domain to
query, so there's no real traffic data to capture. The shape is real; the
story it tells is invented for test coverage.

## Files

- `domain_stats_query_200.json` — a normal `domainStats:query` response,
  with a deliberate gap (no row for one date in the requested range) to
  exercise "gaps are normal, not errors."
- `domain_stats_query_paginated_page1.json` / `_page2.json` — a
  paginated response pair (`nextPageToken` on page 1).
- `domain_stats_query_malformed.json` — a row missing a required field.
- `compliance_status_needs_work.json` — `deliverabilityStatusVerdict.state
  == NEEDS_WORK`, `reason == SPAM_RATE_HIGH` — the hard-gate case.
- `compliance_status_compliant.json` — all three verdicts `COMPLIANT`.
- `compliance_status_missing_verdict.json` — `oneClickUnsubscribeVerdict`
  absent from the response entirely (not every verdict is always present).
- `verification_token_200.json` — a normal `getVerificationToken` response.
- `rate_limited_429.json` — a generic 429 body.
