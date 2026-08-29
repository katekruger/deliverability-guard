# Contributing

This is currently a solo project in active early development. Issues are
welcome; unsolicited large PRs are likely to be rejected on scope grounds
even if the code is correct — open an issue first for anything beyond a small
fix.

## Setup

```bash
gh repo clone katekruger/deliverability-guard
cd deliverability-guard
uv sync
uv run pre-commit install
cp .env.example .env      # fill in your own values; never commit this file
cp config/thresholds.example.yml config/thresholds.yml
```

Run the full check before opening a PR:

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

## Use of AI

AI-assisted contributions are welcome. Disclose it in the PR body: which
model, which harness, and what was and wasn't AI-generated. Review the diff
yourself before opening the PR — "the agent wrote it" is not a defense for
code you didn't understand well enough to explain. See `AGENTS.md` for the
house rules an agent working in this repo is expected to follow.

## What Gets Rejected

- Direct commits to `main`.
- PRs that reformat or refactor code unrelated to the change.
- New dependencies without a one-line justification in the PR body.
- Anything that coerces missing/absent data to zero — see `AGENTS.md`.
- A breaker or provider code path that can pause or throttle without an
  explicit `dry_run=False`.
- Overclaiming: any statistical claim the data can't support, especially
  around low-volume complaint rates. This project's credibility depends on
  saying "we don't know" out loud. See `BUILD-PLAN.md` §6.
- Live API calls in tests. Fixtures only.
- PRs that remove or weaken a test to make CI pass.

### Why Issues Rather Than Pull Requests

For anything larger than a small, obviously-correct fix, open an issue
describing the problem or proposal first. This project's design has a lot of
non-obvious constraints (see `BUILD-PLAN.md` and `AGENTS.md`) and a PR built
on a wrong assumption is more expensive to review than a short conversation
up front.
