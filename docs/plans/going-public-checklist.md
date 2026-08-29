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

- [ ] PyPI Trusted Publisher configured **before** tagging
- [ ] `release.yml` workflow verified end-to-end against a pre-release tag
- [ ] Terminal GIF recorded and embedded in the README
