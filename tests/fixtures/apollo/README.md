# Apollo fixtures — provenance

Same caveat as `tests/fixtures/instantly/README.md`: these are hand-authored
from Apollo's public API documentation, not captured from a live account,
since this environment has no Apollo credentials. Verify against a real
captured-and-redacted response before trusting this driver against
production traffic.

## Files

- `daily_stats_200.json` — a normal `GET /emailer_campaigns/{id}/daily_stats` response.
- `daily_stats_malformed.json` — a response missing a required field (`sender_email`).
- `email_accounts_200.json` — a normal `GET /email_accounts` response.
- `rate_limited_429.json` — a generic 429 body, used to exercise the retry path.
