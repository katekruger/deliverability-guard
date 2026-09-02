# Going-public checklist

The repo started **private** and flipped to **public on 2026-08-29** (this
file's own status below was left stale through several later PRs -- fixed on
2026-09-02, per `AGENTS.md` item 5: a checkable fact restated in only one
place doesn't drift, so from here on this file is the single source of
truth for go-public status; don't re-derive it from memory elsewhere).

## Before flipping to public

- [x] Repo visibility: private → public
- [x] Secret scanning: on
- [x] Push protection: on
- [x] CodeQL default setup: on (languages: `python`, `actions`)
- [x] Private vulnerability reporting: on
- [x] Dependabot alerts + security updates: on
- [x] `ci.yml` expanded to a Python 3.12/3.13 matrix
- [ ] Social preview image set (Settings → General → Social preview) --
      still not done; needs an actual image asset, which nobody has made
- [x] `LICENSE`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`
      confirmed to read correctly as public-facing docs
- [x] Branch protection on `main`: required status checks
      (`lint, format, types, tests` × 3.12/3.13, `zizmor GitHub Actions
      security audit`), force-push and branch deletion disallowed.
      **Required PR review count is currently 0**, not the 1 originally
      planned -- removed after PR #6 hit exactly the friction you'd expect
      on a solo-maintained project with no second reviewer
      (`enforce_admins: false` was the intended escape hatch for that, but
      an actual required-reviewer count of 0 is more honest about there
      being no reviewer than an admin-bypass-every-time setup would be).
      If a second maintainer ever joins, turn this back on.
- [x] No secrets, API responses, or internal notes found in commit history
      from the private period

## Release history

- **v0.1.0** (2026-08-29): the posterior engine, Instantly and Smartlead
  provider drivers, the breaker/ladder/decision-log, and Postmaster Tools
  v2 + the identity scheme. Published to PyPI via Trusted Publishing;
  GitHub Release created by `release.yml`.
- **v0.2.0** (2026-09-01): SES, Apollo, and Lemlist provider drivers;
  Spamhaus DQS and DMARC signals; warmup curve adherence; a read-only MCP
  server; a full CLI/daemon (`run`/`check`/`status`/`resume`); 11 rounds of
  audit-driven correctness fixes. Also published via Trusted Publishing.
- [x] PyPI Trusted Publisher configured (confirmed working across both
      releases above -- OIDC only, no API token secret ever existed in this
      repo)
- [x] `release.yml` verified end-to-end against two real tag pushes
- [x] Terminal GIF recorded (`docs/demo.gif`) and embedded in the README

## Still open

- [ ] Social preview image (see above)
- [ ] `awesome-mcp-servers` listing -- unblocked now that `mcp_server.py`
      exists (v0.2.0), not yet submitted
