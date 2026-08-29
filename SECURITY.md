# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities using
[GitHub's private vulnerability reporting](https://github.com/katekruger/deliverability-guard/security/advisories/new)
for this repository (Security tab → Report a vulnerability). Do not open a
public issue for a security report.

You should expect an initial response within a few days. This is currently a
solo-maintained project, so timelines are best-effort, not contractual.

## Scope Notes Specific to This Project

- This tool can be configured to hold credentials for third-party sending
  platforms (e.g. an Instantly API key). Never commit real credentials to a
  fixture, test, or example — see `AGENTS.md`.
- One provider driver in scope (Smartlead) authenticates via an API key in
  the query string, which risks leaking into server/proxy logs and `Referer`
  headers. If you find a place we log a full request URL for that provider,
  that is itself a security bug — please report it.
- Dry-run is the default everywhere. If you find a code path that can pause
  or throttle a real mailbox without `dry_run=False` explicitly set, treat it
  as a security-relevant bug, not just a correctness one.
