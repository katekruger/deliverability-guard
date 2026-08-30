"""Command-line entry point.

BUILD-PLAN.md §10 describes the intended shape; this module ships the
minimum viable slice of it (audit finding ENG-6): `check` is the
single-shot form of the fast loop and the smallest thing a user can put in
cron to get a real, running system rather than a library nobody invokes.
`status` and `resume` are the read and human-review-gate paths (ADR 0003)
-- `resume` is the ONLY way a paused mailbox becomes callable again.

Provider credentials are read from the environment (`AGENTS.md`: no
secrets in the repo, ever) -- never from the YAML config `config.py` loads.
The full two-loop daemon controller (`loops/fast.py`, `loops/slow.py`, run
continuously) is out of scope for this slice; `check` deliberately reuses
`loops.fast`'s underlying `engine.breaker.evaluate` so the eventual daemon
and this command cannot drift apart.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from deliverability_guard.audit.log import DecisionRecord, append_record
from deliverability_guard.config import AppConfig, ConfigError, load_config
from deliverability_guard.engine.breaker import (
    BreakerStateStore,
    BreakerStateStoreLoadError,
    MailboxBreakerStatus,
    Verdict,
    evaluate,
)
from deliverability_guard.providers.base import MailboxRef, MailboxStatus, ProviderDriver
from deliverability_guard.providers.instantly import InstantlyDriver

_DEFAULT_CONFIG_PATH = Path("config/thresholds.yml")

# Exit codes. 0: success, nothing to report. 1: `check` found a breaching
# mailbox, or `resume` was refused (both are "the command ran fine but the
# answer is bad news" -- distinct from 2, "the command itself couldn't
# run").
_EXIT_OK = 0
_EXIT_BREACH_OR_REFUSED = 1
_EXIT_CONFIG_OR_SETUP_ERROR = 2


class CliError(Exception):
    """A CLI-level setup error with a clean, user-facing message -- e.g. an
    unknown provider name, or a required credential missing from the
    environment. Never a traceback a cron job's error email has to explain."""


def build_driver(provider: str, *, env: Mapping[str, str]) -> ProviderDriver:
    """The provider registry. Currently just Instantly -- see
    BUILD-PLAN.md §3's v0.1 scope. Extend this, not `main()`, to add a
    provider."""
    if provider == "instantly":
        api_key = env.get("INSTANTLY_API_KEY")
        if not api_key:
            raise CliError("INSTANTLY_API_KEY is not set (see .env.example)")
        return InstantlyDriver(api_key=api_key)
    raise CliError(f"unknown provider {provider!r}")


def cmd_check(
    *,
    driver: ProviderDriver,
    config: AppConfig,
    state_store: BreakerStateStore,
    now: datetime,
    out: TextIO,
) -> int:
    """Evaluate every mailbox the driver reports on, print one verdict line
    per mailbox, and append a decision record for each. Exit non-zero if
    any mailbox's verdict is not OK -- WARN included, since "notify only"
    is still something a cron job's caller should see, not silently absorb.

    Aggregates each mailbox's stats over the last day. A `DISCONNECTED` day
    (see `providers.base.MailboxStatus`) is an outage, not evidence, and is
    excluded from the aggregate entirely rather than folded in as 0
    sends/0 bounces -- the same missing-data-as-zero coercion AGENTS.md
    prohibits everywhere else in this project.
    """
    since = now.date() - timedelta(days=1)
    day_stats = driver.read_mailbox_stats(since)

    totals: dict[MailboxRef, tuple[int, int]] = {}
    for stat in day_stats:
        if stat.status is MailboxStatus.DISCONNECTED:
            continue
        prior_sends, prior_complaints = totals.get(stat.mailbox, (0, 0))
        totals[stat.mailbox] = (prior_sends + stat.sends, prior_complaints + stat.bounces)

    if not totals:
        print("no mailboxes reported any stats", file=out)
        return _EXIT_OK

    exit_code = _EXIT_OK
    for mailbox, (sends, complaints) in sorted(totals.items(), key=lambda kv: kv[0].mailbox_id):
        result = evaluate(
            driver=driver,
            mailbox=mailbox,
            sends=sends,
            complaints=complaints,
            prior=config.prior,
            thresholds=config.thresholds,
            state_store=state_store,
            dry_run=config.dry_run,
            now=now,
        )
        append_record(config.decision_log_path, DecisionRecord.from_evaluation(result))
        print(
            f"{mailbox.mailbox_id}: {result.verdict.name} (sends={sends}, complaints={complaints})",
            file=out,
        )
        if result.verdict is not Verdict.OK:
            exit_code = _EXIT_BREACH_OR_REFUSED
    return exit_code


def cmd_status(
    *, mailboxes: Sequence[MailboxRef], state_store: BreakerStateStore, out: TextIO
) -> int:
    """Print each mailbox's current breaker state. Always exits 0 -- an
    unknown mailbox correctly prints ACTIVE (see `BreakerStateStore.status_of`),
    which is informative, not an error."""
    for mailbox in mailboxes:
        print(f"{mailbox.mailbox_id}: {state_store.status_of(mailbox).name}", file=out)
    return _EXIT_OK


def cmd_resume(*, mailbox: MailboxRef, state_store: BreakerStateStore, out: TextIO) -> int:
    """The only path out of PAUSED (ADR 0003) -- a typed, explicit human
    action, never something `check` or any other command reaches on its
    own. Refuses (exit 1, not an error) for a mailbox that isn't currently
    PAUSED, so a mistyped mailbox id or an already-resumed mailbox doesn't
    look like success."""
    status = state_store.status_of(mailbox)
    if status is not MailboxBreakerStatus.PAUSED:
        print(
            f"{mailbox.mailbox_id} is not paused (status: {status.name}); nothing to resume",
            file=out,
        )
        return _EXIT_BREACH_OR_REFUSED
    state_store.resume_after_human_review(mailbox)
    print(f"{mailbox.mailbox_id} resumed after human review", file=out)
    return _EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deliverability-guard",
        description="A sending circuit breaker for outbound email.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"path to the thresholds config (default: {_DEFAULT_CONFIG_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="evaluate every mailbox once and print verdicts")

    status_parser = subparsers.add_parser("status", help="print current breaker state per mailbox")
    status_parser.add_argument("mailbox_id", nargs="+", help="one or more mailbox addresses")

    resume_parser = subparsers.add_parser(
        "resume", help="resume a paused mailbox after human review (ADR 0003)"
    )
    resume_parser.add_argument("mailbox_id", help="the mailbox address to resume")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_OR_SETUP_ERROR

    try:
        state_store = BreakerStateStore.from_log(config.decision_log_path)
    except BreakerStateStoreLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_OR_SETUP_ERROR

    if args.command == "check":
        try:
            driver = build_driver(config.provider, env=os.environ)
        except CliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_CONFIG_OR_SETUP_ERROR
        return cmd_check(
            driver=driver,
            config=config,
            state_store=state_store,
            now=datetime.now(UTC),
            out=sys.stdout,
        )

    if args.command == "status":
        mailboxes = [MailboxRef(provider=config.provider, mailbox_id=m) for m in args.mailbox_id]
        return cmd_status(mailboxes=mailboxes, state_store=state_store, out=sys.stdout)

    if args.command == "resume":
        mailbox = MailboxRef(provider=config.provider, mailbox_id=args.mailbox_id)
        return cmd_resume(mailbox=mailbox, state_store=state_store, out=sys.stdout)

    raise AssertionError(  # pragma: no cover
        f"unreachable: argparse required a valid command, got {args.command!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
