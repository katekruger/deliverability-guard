# lemlist fixtures — provenance

Same caveat as `tests/fixtures/instantly/README.md`: these are hand-authored
from lemlist's public API documentation, not captured from a live account,
since this environment has no lemlist credentials. Verify against a real
captured-and-redacted response before trusting this driver against
production traffic.

## Files

- `export_activities_200.json` — a normal `GET /campaigns/{id}/export?type=activities` response.
- `export_activities_malformed.json` — a response missing a required field (`sendUserEmail`).
- `rate_limited_429.json` — a generic 429 body, used to exercise the retry path.
