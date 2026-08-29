# Smartlead fixtures — provenance

Same caveat as `tests/fixtures/instantly/README.md`: these are hand-authored
from Smartlead's public API documentation, not captured from a live
account, since this environment has no Smartlead credentials. Verify against
a real captured-and-redacted response before trusting this driver against
production traffic.

## Files

- `campaign_statistics_200.json` — a normal `GET /campaigns/{id}/statistics` response.
- `campaign_statistics_malformed.json` — a response missing a required field.
- `email_account_update_200.json` — a normal `POST /email-accounts/{id}` (throttle) response.
- `campaign_status_200.json` — a normal `PATCH /campaigns/{id}/status` response.
- `rate_limited_429.json` — a generic 429 body, used to exercise the retry path.
