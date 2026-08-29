# AGENTS.md

## Stop and read this before you write code

This repo has conventions. Violating them wastes a review cycle.

## Commands

- Install: `uv sync`
- Test: `uv run pytest`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Types: `uv run pyright`
- All of it: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`

## Layout

- `src/deliverability_guard/` — the package. `tests/` mirrors it. Never put tests inside the package.
- `docs/decisions/` — ADRs (MADR 4). Permanent, numbered, never renumbered.
- `docs/plans/` — dated design plans. Disposable once executed.
- `config/thresholds.example.yml` — the default ladder. Copy to `thresholds.yml` to use.

## Non-negotiable

1. **Never commit directly to `main`.** Branch, commit, open a PR. Even for a typo.
2. **Tests before implementation.** If you are adding behavior, the failing test comes first.
3. **No secrets in the repo, ever** — not in tests, not in fixtures, not in examples. Use env vars and `.env.example`.
4. **Never re-type a file's contents from tool output.** Output can be truncated. Edit in place.
5. **Every dependency added needs a one-line justification in the PR body.**
6. **If a decision is expensive to reverse, write an ADR in the same PR.**
7. **Linter versions are pinned exactly.** Do not float them to fix a failure — fix the code, or bump deliberately in its own PR.

## Project-specific non-negotiables

1. **DRY RUN IS THE DEFAULT.** Any code path that can pause or throttle a real
   mailbox must be off unless explicitly enabled. A tool that stops someone's
   revenue by accident is worse than no tool. Dry-run must produce decisions
   identical to the live path — implement it as a no-op provider decorator,
   never as a separate logic branch.
2. **NEVER coerce missing data to zero.** Absence of complaint data is
   `INSUFFICIENT_DATA`, never `OK`. This is the bug that makes monitoring go
   dark exactly when things are worst — a throttled domain sends less, can
   drop below a provider's privacy threshold, and disappear from reporting
   entirely.
3. **Never claim a statistical property the data cannot support.** Under
   roughly 1,000 sends/day/provider this is a leading-indicator monitor, not a
   statistically valid complaint-rate breaker. Say so in the README and never
   soften it.
4. **No live API calls in tests.** Recorded fixtures only.

## Before opening a PR

- [ ] The full check command above passes locally
- [ ] `CHANGELOG.md` has an entry under `## Unreleased`
- [ ] No new file lacks a test, or the PR says why
- [ ] The PR body discloses: model, harness, and that it was AI-assisted
- [ ] You showed the human the full diff and got approval

## What gets rejected

- Direct commits to `main`
- Reformatting unrelated code
- New dependencies without justification
- "Improvements" nobody asked for, bundled into an unrelated PR
- Removing a test to make CI pass
- Anything that makes an approval gate optional
- A breaker/provider code path that can act (pause/throttle) without an explicit `dry_run=False`
- Code that treats missing data as zero, anywhere in the pipeline
