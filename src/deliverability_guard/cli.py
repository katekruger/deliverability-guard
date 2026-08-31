"""Command-line entry point.

BUILD-PLAN.md §10 describes the intended shape. `check` is the single-shot
form of the fast loop -- the smallest thing a user can put in cron to get a
real, running system rather than a library nobody invokes. `run` is the
full always-on form: the two-loop daemon (`loops/controller.py`) running
until stopped. `status` and `resume` are the read and human-review-gate
paths (ADR 0003) -- `resume` is the ONLY way a paused mailbox becomes
callable again.

Provider credentials are read from the environment (`AGENTS.md`: no
secrets in the repo, ever) -- never from the YAML config `config.py` loads.
`check` and `run`'s fast tick both call `loops.fast.evaluate_all_mailboxes`,
so the one-shot and continuous forms cannot drift apart from each other.
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from deliverability_guard.audit.log import (
    DecisionRecord,
    ResumeRecord,
    append_record,
    append_resume_record,
)
from deliverability_guard.config import AppConfig, ConfigError, load_config
from deliverability_guard.engine.breaker import (
    BreakerEvaluation,
    BreakerStateStore,
    BreakerStateStoreLoadError,
    MailboxBreakerStatus,
    ThresholdStore,
    Verdict,
)
from deliverability_guard.loops import controller
from deliverability_guard.loops.fast import evaluate_all_mailboxes
from deliverability_guard.loops.slow import ThresholdAdjustment
from deliverability_guard.providers.base import MailboxRef, ProviderDriver
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

    Aggregates each mailbox's stats over the last day via
    `loops.fast.evaluate_all_mailboxes` -- the same aggregation-and-
    evaluation path `loops.controller`'s fast tick uses, so this command
    and the continuous daemon cannot drift apart from each other. That
    includes hierarchical pooling and a CUSUM trend check (`max_pooled_ess`
    from config; CUSUM state starts fresh each invocation, since `check` is
    a one-shot process with nowhere to persist a running trend statistic
    between invocations -- `run` is where CUSUM accumulates real history).
    """
    since = now.date() - timedelta(days=1)
    results = evaluate_all_mailboxes(
        driver=driver,
        since=since,
        prior=config.prior,
        thresholds=config.thresholds,
        state_store=state_store,
        dry_run=config.dry_run,
        now=now,
        max_pooled_ess=config.max_pooled_ess,
        cusum_states={},
    )

    if not results:
        print("no mailboxes reported any stats", file=out)
        return _EXIT_OK

    exit_code = _EXIT_OK
    for result in results:
        append_record(config.decision_log_path, DecisionRecord.from_evaluation(result))
        print(
            f"{result.mailbox.mailbox_id}: {result.verdict.name} "
            f"(sends={result.sends}, complaints={result.complaints})",
            file=out,
        )
        if result.verdict is not Verdict.OK:
            exit_code = _EXIT_BREACH_OR_REFUSED
    return exit_code


def cmd_run(
    *,
    driver: ProviderDriver,
    config: AppConfig,
    state_store: BreakerStateStore,
    threshold_store: ThresholdStore,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    out: TextIO,
    max_ticks: int | None = None,
) -> int:
    """The always-on form of `check`: runs `loops.controller.run` with this
    process's real config, printing one line per fast-tick verdict and one
    line per slow-tick threshold adjustment, and appending a decision
    record for every fast-tick evaluation exactly like `check` does.

    `max_ticks=None` (the default; `main` never passes anything else) runs
    until the caller interrupts it -- `main` catches `KeyboardInterrupt`
    around this call so Ctrl-C is a clean shutdown, not a traceback. Tests
    pass a small `max_ticks` instead of relying on an interrupt.
    """

    def on_fast_tick(results: list[BreakerEvaluation]) -> None:
        for result in results:
            append_record(config.decision_log_path, DecisionRecord.from_evaluation(result))
            print(
                f"[fast] {result.mailbox.mailbox_id}: {result.verdict.name} "
                f"(sends={result.sends}, complaints={result.complaints})",
                file=out,
            )

    def on_slow_tick(adjustment: ThresholdAdjustment) -> None:
        print(f"[slow] tightened thresholds: {adjustment.reason}", file=out)

    controller.run(
        driver=driver,
        prior=config.prior,
        dry_run=config.dry_run,
        state_store=state_store,
        threshold_store=threshold_store,
        fast_interval=timedelta(seconds=config.fast_interval_seconds),
        slow_interval=timedelta(seconds=config.slow_interval_seconds),
        now=now,
        sleep=sleep,
        max_ticks=max_ticks,
        max_pooled_ess=config.max_pooled_ess,
        on_fast_tick=on_fast_tick,
        on_slow_tick=on_slow_tick,
    )
    return _EXIT_OK


def cmd_status(
    *, mailboxes: Sequence[MailboxRef], state_store: BreakerStateStore, out: TextIO
) -> int:
    """Print each mailbox's current breaker state. Always exits 0 -- an
    unknown mailbox correctly prints ACTIVE (see `BreakerStateStore.status_of`),
    which is informative, not an error."""
    for mailbox in mailboxes:
        print(f"{mailbox.mailbox_id}: {state_store.status_of(mailbox).name}", file=out)
    return _EXIT_OK


def cmd_resume(
    *,
    mailbox: MailboxRef,
    state_store: BreakerStateStore,
    decision_log_path: Path,
    resumed_by: str,
    now: datetime,
    out: TextIO,
) -> int:
    """The only path out of PAUSED (ADR 0003) -- a typed, explicit human
    action, never something `check` or any other command reaches on its
    own. Refuses (exit 1, not an error) for a mailbox that isn't currently
    PAUSED, so a mistyped mailbox id or an already-resumed mailbox doesn't
    look like success.

    Appends a `ResumeRecord` to the decision log so this survives a process
    restart -- see `engine.breaker.BreakerStateStore.from_log`. `resumed_by`
    is required, not defaulted, so a human is always named in the log: ADR
    0003's whole point is that a human is on the hook for this decision.
    """
    status = state_store.status_of(mailbox)
    if status is not MailboxBreakerStatus.PAUSED:
        print(
            f"{mailbox.mailbox_id} is not paused (status: {status.name}); nothing to resume",
            file=out,
        )
        return _EXIT_BREACH_OR_REFUSED
    state_store.resume_after_human_review(mailbox)
    append_resume_record(
        decision_log_path,
        ResumeRecord(
            resumed_at=now,
            provider=mailbox.provider,
            mailbox_id=mailbox.mailbox_id,
            resumed_by=resumed_by,
        ),
    )
    print(f"{mailbox.mailbox_id} resumed after human review (by {resumed_by})", file=out)
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

    run_parser = subparsers.add_parser(
        "run", help="run the two-loop daemon continuously (Ctrl-C to stop)"
    )
    run_parser.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="stop after this many fast-loop ticks instead of running until interrupted",
    )

    status_parser = subparsers.add_parser("status", help="print current breaker state per mailbox")
    status_parser.add_argument("mailbox_id", nargs="+", help="one or more mailbox addresses")

    resume_parser = subparsers.add_parser(
        "resume", help="resume a paused mailbox after human review (ADR 0003)"
    )
    resume_parser.add_argument("mailbox_id", help="the mailbox address to resume")
    resume_parser.add_argument(
        "--by",
        default=None,
        help="who is resuming this mailbox, recorded in the decision log (default: $USER)",
    )

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

    if args.command == "run":
        try:
            driver = build_driver(config.provider, env=os.environ)
        except CliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_CONFIG_OR_SETUP_ERROR
        try:
            return cmd_run(
                driver=driver,
                config=config,
                state_store=state_store,
                threshold_store=ThresholdStore(config.thresholds),
                now=lambda: datetime.now(UTC),
                sleep=time.sleep,
                out=sys.stdout,
                max_ticks=args.ticks,
            )
        except KeyboardInterrupt:
            print("stopped", file=sys.stdout)
            return _EXIT_OK

    if args.command == "status":
        mailboxes = [MailboxRef(provider=config.provider, mailbox_id=m) for m in args.mailbox_id]
        return cmd_status(mailboxes=mailboxes, state_store=state_store, out=sys.stdout)

    if args.command == "resume":
        mailbox = MailboxRef(provider=config.provider, mailbox_id=args.mailbox_id)
        resumed_by = args.by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        return cmd_resume(
            mailbox=mailbox,
            state_store=state_store,
            decision_log_path=config.decision_log_path,
            resumed_by=resumed_by,
            now=datetime.now(UTC),
            out=sys.stdout,
        )

    raise AssertionError(  # pragma: no cover
        f"unreachable: argparse required a valid command, got {args.command!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
