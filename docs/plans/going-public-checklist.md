# Going-public checklist

This repo starts **private**. Several security and CI defaults behave
differently on a private repo (metered Actions minutes, GHAS-gated secret
scanning) than they will once this flips public. Do this work at flip time,
not now, but keep the list here so it isn't reinvented then.

Planned for **Prompt 5** (release), when v0.1.0 is ready to ship.

## Before flipping to public

- [ ] Repo visibility: private → public (Settings → Danger Zone)
- [ ] Secret scanning: on (free automatically once public)
- [ ] Push protection: on
- [ ] CodeQL default setup: on (Settings → Code security)
- [ ] Private vulnerability reporting: on (already referenced from
      `SECURITY.md`; confirm the toggle itself is enabled)
- [ ] Dependabot alerts + security updates: on
- [ ] Expand `ci.yml` from a single job to a real Python version matrix
      (private-repo Actions minutes are metered; public repos on the free
      tier get much more headroom for public repos)
- [ ] Social preview image set (Settings → General → Social preview)
- [ ] Confirm `LICENSE`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`
      all read correctly as public-facing docs, not written assuming a
      private, single-maintainer context
- [ ] Branch protection on `main`: require PR review, require the CI status
      check, disallow force-push
- [ ] Confirm no secrets, API responses, or internal notes ended up in commit
      history while the repo was private (a private repo is not a good place
      to be sloppy about this, but double-check before it's public)

## At release time (v0.1.0), also see Prompt 5

- [ ] PyPI Trusted Publisher configured **before** tagging (human-only: needs
      a PyPI account and can't be done from this repo)
- [x] `release.yml` written (tag-triggered, verifies tag/CHANGELOG match,
      calls `ci.yml`, builds, publishes via Trusted Publishing into a
      `release` environment requiring manual approval, creates the GitHub
      Release) -- **not yet verified end-to-end against a real tag push**,
      since that requires the PyPI Trusted Publisher to exist first
- [x] Terminal GIF recorded (`docs/demo.gif`, via `examples/demo.py` +
      `examples/demo.tape`) and embedded in the README
- [ ] Expand `ci.yml` to a real Python version matrix -- deliberately NOT
      done yet: the repo is still private, and doing this before the
      visibility flip spends metered private-repo Actions minutes for no
      benefit. Do this at the same time as the flip, not before.
