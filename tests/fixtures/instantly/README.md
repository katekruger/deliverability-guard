# Instantly fixtures — provenance

**These are hand-authored, not captured from a live account.** BUILD-PLAN.md
Prompt 2 asks for real response shapes "captured from Instantly once by
hand, redacted." This environment has no Instantly account or API
credentials, so that capture couldn't be done. Instead, these fixtures were
constructed from Instantly's public API documentation (endpoint shapes,
field names) and ordinary REST/JSON conventions, and are clearly labeled as
such here rather than presented as verified.

**Before trusting this driver against production traffic**, replace these
with an actual captured-and-redacted response from a real account, and
correct `providers/instantly.py`'s parsing if the real shape differs from
what's assumed here (most likely candidates: the exact field names in a
daily-analytics row, and whether dates come back as bare dates or full
timestamps with an offset).

## Files

- `analytics_daily_200.json` — a normal `GET /api/v2/accounts/analytics/daily` response.
- `analytics_daily_malformed.json` — a response missing a required field, for
  `MalformedResponseError` coverage.
- `pause_account_200.json` — a normal `POST /api/v2/accounts/{email}/pause` response.
- `rate_limited_429.json` — a generic 429 body, used to exercise the retry path.
