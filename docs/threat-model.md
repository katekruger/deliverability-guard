# Threat model: provider credentials and driver boundaries

This covers the credential and logging risks specific to the provider
drivers (`src/deliverability_guard/providers/`). It is not a general
security policy -- see [SECURITY.md](../SECURITY.md) for how to report a
vulnerability.

## Smartlead: API key in the query string

Smartlead's API authenticates every request via `?api_key=...` in the query
string, not a header (BUILD-PLAN.md §5). This is a real, structural risk
independent of anything this codebase does right or wrong:

- **Server access logs.** Most web servers and reverse proxies log the full
  request path including the query string by default.
- **Proxy/CDN logs.** Any intermediary between this process and
  `server.smartlead.ai` (a corporate proxy, an egress gateway) can log the
  same thing.
- **`Referer` headers.** If a response from this call ever triggers a
  browser-originated follow-up request (it shouldn't, in this codebase's
  server-to-server usage, but this is a driver other code will build on),
  the `Referer` header can carry the full URL, key included, to a third
  party.

**Mitigation implemented in `providers/smartlead.py`:** no code path in that
module logs, raises, or otherwise surfaces `response.request.url` or
`str(response.url)`. Every error message and `ActionResult.detail` string is
built from a hardcoded, literal description of the endpoint (e.g.
`"campaign statistics returned status 500"`), never from the request or
response's URL object. `tests/test_smartlead.py` enforces this with two
tests that assert the driver's own API key never appears in a raised
exception message or a returned `ActionResult.detail`, while separately
confirming the key really was sent (so the test is checking the actual
leak path, not a vacuous one).

**What this does NOT cover:** infrastructure-level logging (your own
server's access logs, any proxy in front of this process) is out of this
codebase's control. If you operate this driver in production, treat
Smartlead's own request logs as containing a live credential and configure
log redaction or access controls accordingly -- this is a limitation of
Smartlead's API design, not something a client library can fully paper over.

## All providers: credential storage

Every provider driver takes its credential (`api_key`) as a constructor
argument, sourced from an environment variable (see `.env.example`) at the
call site -- never hardcoded, never committed, never read from a config file
checked into the repo. `AGENTS.md` requires this project-wide.

## Decision log content (forward-looking)

The breaker's decision log (`audit/log.py`, Prompt 3) will record every
evaluation's inputs and outputs for reproducibility. Provider driver
`ActionResult.detail` strings are designed to be safe to write to that log
verbatim -- which is precisely why the Smartlead URL-safety property above
matters at the driver layer and not just at the logging layer: by the time
a string reaches the decision log, it needs to already be safe.
