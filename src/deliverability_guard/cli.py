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

Exit codes (CLOSE-5b): 0 all clear, 1 `check` found a breach (or `resume`
was refused), 2 a config/setup error (bad YAML, unknown provider, missing
credential), 3 a provider transport failure (network error, rate limit
exhausted, malformed response) OR a decision-log write failure -- both
`resume`'s `append_resume_record` call (CLOSE9-2) and `check`/`run`'s own
per-mailbox `append_record` calls (CLOSE10-2) write to the same decision
log and can fail the same ways (a read-only mount, a full disk, a
directory owned by a different user); each gets its own message naming
the write failure specifically, distinct from a real provider failure, so
the catch-all's generic wording stays reserved for something genuinely
unanticipated -- and exit 3 must never collide with exit 1, which already
means "refused." Distinct from 1 so a cron wrapper can tell "the fleet is
healthy" apart from "we couldn't even ask the provider" or "we couldn't
record what we did."
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

import httpx
from botocore.exceptions import NoRegionError

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
from deliverability_guard.providers.apollo import ApolloCampaignDriver, ApolloDriver
from deliverability_guard.providers.base import MailboxRef, ProviderDriver, ProviderError
from deliverability_guard.providers.instantly import InstantlyDriver
from deliverability_guard.providers.lemlist import LemlistCampaignDriver, LemlistDriver
from deliverability_guard.providers.noop import NoopDriver
from deliverability_guard.providers.ses import SesConfigurationSetDriver, SesDriver
from deliverability_guard.providers.smartlead import SmartleadCampaignDriver, SmartleadDriver

_DEFAULT_CONFIG_PATH = Path("config/thresholds.yml")

# Exit codes. See the module docstring's "Exit codes" paragraph above for
# what each one means to a caller.
_EXIT_OK = 0
_EXIT_BREACH_OR_REFUSED = 1
_EXIT_CONFIG_OR_SETUP_ERROR = 2
_EXIT_PROVIDER_TRANSPORT_FAILURE = 3


class CliError(Exception):
    """A CLI-level setup error with a clean, user-facing message -- e.g. an
    unknown provider name, or a required credential missing from the
    environment. Never a traceback a cron job's error email has to explain."""


def _format_verdict_line(prefix: str, result: BreakerEvaluation) -> str:
    """The one line printed per mailbox by both `cmd_check` and `cmd_run`'s
    fast tick (CLOSE6-4). A non-OK verdict with an `action` appends that
    action's own `detail` -- e.g. ADR 0003's "mailbox is paused; throttle
    refused pending human review." Before this, a PAUSED mailbox whose
    evidence kept landing in the THROTTLE band printed a bare `THROTTLE`
    forever on stdout, with the honest refusal detail visible only in the
    decision log -- exactly the confusing-to-an-operator output CLOSE5-4's
    reproduction was about, just not yet reflected in what `check`/`run`
    actually print. `action` is `None` for OK and WARN (neither ever calls
    `_act`), so this only ever adds text for THROTTLE/PAUSE."""
    line = (
        f"{prefix}{result.mailbox.mailbox_id}: {result.verdict.name} "
        f"(sends={result.sends}, complaints={result.complaints})"
    )
    if result.action is not None:
        line += f" -- {result.action.detail}"
    return line


def build_driver(provider: str, *, env: Mapping[str, str]) -> ProviderDriver:
    """The provider registry. Extend this, not `main()`, to add a provider.

    `smartlead` proves the THROTTLE path (BUILD-PLAN.md §5's capability
    matrix) -- it was fully implemented in `providers/smartlead.py` but
    unreachable from the CLI until CLOSE-5a registered it here. `lemlist`,
    `apollo`, and `ses` follow the same *CampaignDriver/*ConfigurationSetDriver
    adapter pattern Smartlead established -- each pins the campaign id (or,
    for SES, the configuration set) their own `read_mailbox_stats` needs but
    the generic `ProviderDriver` Protocol has no room for -- registered here
    for the same reason Smartlead was (CLOSE3-4). `noop` requires no
    credential and reports no mailboxes; it exists so `check`/`run` can be
    exercised end to end -- config loading, the decision log, exit codes --
    without a live provider account (CLOSE-5a).
    """
    if provider == "instantly":
        api_key = env.get("INSTANTLY_API_KEY")
        if not api_key:
            raise CliError("INSTANTLY_API_KEY is not set (see .env.example)")
        return InstantlyDriver(api_key=api_key)
    if provider == "smartlead":
        api_key = env.get("SMARTLEAD_API_KEY")
        if not api_key:
            raise CliError("SMARTLEAD_API_KEY is not set (see .env.example)")
        campaign_id = env.get("SMARTLEAD_CAMPAIGN_ID")
        if not campaign_id:
            raise CliError("SMARTLEAD_CAMPAIGN_ID is not set (see .env.example)")
        return SmartleadCampaignDriver(
            inner=SmartleadDriver(api_key=api_key), campaign_id=campaign_id
        )
    if provider == "lemlist":
        api_key = env.get("LEMLIST_API_KEY")
        if not api_key:
            raise CliError("LEMLIST_API_KEY is not set (see .env.example)")
        campaign_id = env.get("LEMLIST_CAMPAIGN_ID")
        if not campaign_id:
            raise CliError("LEMLIST_CAMPAIGN_ID is not set (see .env.example)")
        return LemlistCampaignDriver(inner=LemlistDriver(api_key=api_key), campaign_id=campaign_id)
    if provider == "apollo":
        api_key = env.get("APOLLO_API_KEY")
        if not api_key:
            raise CliError("APOLLO_API_KEY is not set (see .env.example)")
        campaign_id = env.get("APOLLO_CAMPAIGN_ID")
        if not campaign_id:
            raise CliError("APOLLO_CAMPAIGN_ID is not set (see .env.example)")
        return ApolloCampaignDriver(inner=ApolloDriver(api_key=api_key), campaign_id=campaign_id)
    if provider == "ses":
        configuration_set_name = env.get("SES_CONFIGURATION_SET_NAME")
        if not configuration_set_name:
            raise CliError("SES_CONFIGURATION_SET_NAME is not set (see .env.example)")
        # No API key: SES authenticates via boto3's normal AWS credential
        # chain (env vars, shared config, instance role, ...), not a
        # provider-issued key. `AWS_REGION` is read explicitly rather than
        # relying on boto3's own ambient region resolution, so this
        # constructs deterministically from the same config source
        # everything else here reads from.
        region_name = env.get("AWS_REGION")
        try:
            inner = SesDriver(region_name=region_name)
        except NoRegionError as exc:
            # CLOSE8-2: `boto3.client(...)` raises this at CONSTRUCTION time
            # (region resolution, unlike credential resolution, happens
            # immediately, not on the first real request) when no region is
            # configured anywhere -- env, shared config, or here. A missing
            # region is a setup problem exactly like a missing credential
            # just above, not a live provider failure, so it gets the same
            # exit code as every other "you forgot to configure something"
            # case in this function, not a bare traceback.
            raise CliError(
                "AWS_REGION is not set and no region is configured elsewhere (see .env.example)"
            ) from exc
        return SesConfigurationSetDriver(
            inner=inner,
            configuration_set_name=configuration_set_name,
        )
    if provider == "noop":
        return NoopDriver()
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

    Each mailbox's decision record is appended via `on_evaluation` --
    immediately after THAT mailbox is evaluated, not in a separate loop
    after every mailbox in the fleet has been (CLOSE6-1). Evaluating the
    whole fleet first and appending records afterward meant a later
    mailbox's evaluation raising (a provider transport failure, e.g. mid-
    `pause()`) left NO record durable for the tick at all -- including an
    earlier mailbox whose PAUSE had just been genuinely confirmed at the
    provider, defeating ADR 0003's human-review gate from the other
    direction the moment the process restarted.
    """
    since = now.date() - timedelta(days=1)
    exit_code = _EXIT_OK
    evaluated_any = False

    def on_evaluation(result: BreakerEvaluation) -> None:
        nonlocal exit_code, evaluated_any
        evaluated_any = True
        append_record(config.decision_log_path, DecisionRecord.from_evaluation(result))
        print(_format_verdict_line("", result), file=out)
        if result.verdict is not Verdict.OK:
            exit_code = _EXIT_BREACH_OR_REFUSED

    evaluate_all_mailboxes(
        driver=driver,
        since=since,
        prior=config.prior,
        thresholds=config.thresholds,
        state_store=state_store,
        dry_run=config.dry_run,
        now=now,
        max_pooled_ess=config.max_pooled_ess,
        cusum_states={},
        on_evaluation=on_evaluation,
    )

    if not evaluated_any:
        print("no mailboxes reported any stats", file=out)
        return _EXIT_OK
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

    Each mailbox's decision record is appended via `on_evaluation`, not
    `on_fast_tick` (CLOSE6-1): `on_fast_tick` only runs once a whole tick's
    batch of mailboxes has finished evaluating, so if a later mailbox in
    the same tick raises (a provider transport failure), `on_fast_tick`
    never runs at all for that tick -- losing the decision record for
    every mailbox already evaluated, including one whose PAUSE the
    provider had just genuinely confirmed. `on_evaluation` runs per
    mailbox, immediately, so an earlier mailbox's record survives a later
    one's failure. `on_fast_tick` is kept for its per-tick `[fast] ...`
    console summary only.
    """

    def on_evaluation(result: BreakerEvaluation) -> None:
        append_record(config.decision_log_path, DecisionRecord.from_evaluation(result))

    def on_fast_tick(results: list[BreakerEvaluation]) -> None:
        for result in results:
            print(_format_verdict_line("[fast] ", result), file=out)

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
        on_evaluation=on_evaluation,
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
    own. Also the way an operator clears a persisted THROTTLED mailbox
    (CLOSE3-3) or a mailbox stuck `PAUSE_FAILED` (CLOSE9-1): `from_log` and
    `evaluate()` both now clear THROTTLED and PAUSE_FAILED on their own once
    a recovered mailbox's single-OK recovery evidence makes it into the log (ADR
    0003's addendum), but a mailbox whose own evidence never recovers had
    no path back to ACTIVE at all before CLOSE3-3 (for THROTTLED) and
    CLOSE9-1 (for PAUSE_FAILED) -- the refusal message just said what the
    mailbox wasn't, with nowhere to go from there.

    `PAUSE_FAILED` specifically: a pause attempt that got a definitive
    FAILED from the provider never actually stopped the mailbox, so it is
    not behind ADR 0003's human-review gate the way a confirmed PAUSED is
    -- but before CLOSE9-1, nothing at all could move it off `PAUSE_FAILED`
    (`cmd_resume` refused it; CLOSE-3b's single-OK recovery only checked
    `THROTTLED`), so `throttled_at_limit`'s idempotency memo latched
    forever and the mailbox's THROTTLE rung went permanently inert.

    Refuses (exit 1, not an error) for a mailbox that is none of PAUSED,
    THROTTLED, or PAUSE_FAILED, so a mistyped mailbox id or an
    already-resumed mailbox doesn't look like success -- the refusal
    message names what CAN be resumed, not just what this mailbox isn't.

    Appends a `ResumeRecord` to the decision log so this survives a process
    restart -- see `engine.breaker.BreakerStateStore.from_log`. `resumed_by`
    is required, not defaulted, so a human is always named in the log: ADR
    0003's whole point is that a human is on the hook for this decision.
    """
    status = state_store.status_of(mailbox)
    _RESUMABLE_STATUSES = (
        MailboxBreakerStatus.PAUSED,
        MailboxBreakerStatus.THROTTLED,
        MailboxBreakerStatus.PAUSE_FAILED,
    )
    if status not in _RESUMABLE_STATUSES:
        print(
            f"{mailbox.mailbox_id} is not paused, throttled, or pause-failed "
            f"(status: {status.name}); nothing to resume -- `resume` only acts on "
            f"{', '.join(s.name for s in _RESUMABLE_STATUSES)}",
            file=out,
        )
        return _EXIT_BREACH_OR_REFUSED
    # CLOSE10-4: write the log record BEFORE mutating the in-process store,
    # not after. `state_store` is meant to be a projection of the log
    # (`BreakerStateStore.from_log` rebuilds it from exactly this file), so
    # if `append_resume_record` raises (the write failure CLOSE9-2's own
    # exception handler exists for), the in-memory store must not already
    # have moved to ACTIVE while the log still says PAUSED -- that's a
    # projection that has silently diverged from its own source. Harmless
    # today (this process exits immediately either way, and the next
    # process rebuilds fresh from the log, which is what actually happened
    # -- nothing), but the ordering was backwards for what `state_store` is
    # supposed to be, and "harmless today" is exactly the kind of claim
    # this project's own audits keep finding doesn't survive a future
    # change to how `cmd_resume` or its caller is used.
    append_resume_record(
        decision_log_path,
        ResumeRecord(
            resumed_at=now,
            provider=mailbox.provider,
            mailbox_id=mailbox.mailbox_id,
            resumed_by=resumed_by,
        ),
    )
    state_store.resume_after_human_review(mailbox)
    print(f"{mailbox.mailbox_id} resumed after human review (by {resumed_by})", file=out)
    return _EXIT_OK


_EXIT_CODE_EPILOG = """\
exit codes:
  0  all clear
  1  check found a breach (or resume was refused)
  2  a config/setup error (bad YAML, unknown provider, missing credential)
  3  a provider transport failure (network error, rate limit exhausted,
     malformed response) or a decision-log write failure
"""


def build_parser() -> argparse.ArgumentParser:
    # CLOSE3-6: this project's own commit history claimed exit codes were
    # documented in "the module docstring, README, and --help's exit code
    # map" -- but no `epilog` was ever set, so the rendered `--help` text
    # had no exit-code content at all. This is the one place a cron author
    # actually looks; module docstring and README were already correct.
    parser = argparse.ArgumentParser(
        prog="deliverability-guard",
        description="A sending circuit breaker for outbound email.",
        epilog=_EXIT_CODE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
            return cmd_check(
                driver=driver,
                config=config,
                state_store=state_store,
                now=datetime.now(UTC),
                out=sys.stdout,
            )
        except CliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_CONFIG_OR_SETUP_ERROR
        except (httpx.HTTPError, ProviderError) as exc:
            print(f"error: provider request failed: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE
        except ValueError as exc:
            # CLOSE7-2: a provider can report a data shape `evaluate()`
            # still can't make sense of -- this is the same category as a
            # transport failure from this CLI's own perspective ("we
            # couldn't get a real verdict out of what the provider sent
            # back"), so it gets the same exit code rather than an
            # undocumented traceback.
            print(f"error: could not evaluate provider data: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE
        except OSError as exc:
            # CLOSE10-2: `on_evaluation`'s own `append_record` call (a LOCAL
            # decision-log write, not a provider request) used to fall
            # through to the catch-all below and get the exact same
            # "unexpected failure evaluating provider data" message a real
            # provider failure gets -- true exit code, wrong story: a
            # read-only decision-log directory has nothing to do with the
            # provider. Caught here, specifically, with the same wording
            # CLOSE9-2 already gave `resume`'s identical write path, so the
            # catch-all below keeps its generic wording for things that are
            # genuinely unexpected, not for a local disk CLOSE9-2 already
            # named once.
            print(f"error: could not record a decision: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE
        except Exception as exc:  # CLOSE8-2, see this except's own comment below
            # CLOSE8-2: the CHANGELOG's CLOSE7-2 entry claimed "no future
            # driver bug or malformed response can traceback either
            # command, even one this fix doesn't anticipate" -- measured
            # false. Only `ValueError` was ever caught; a `KeyError`,
            # `TypeError`, `AttributeError`, an unwrapped `RuntimeError`
            # from a driver, or anything else this project's own drivers
            # don't yet anticipate all tracebacked with no exit code,
            # entirely outside the documented 0/1/2/3 map -- the exact
            # thing `CliError`'s docstring, and that claim, both promise
            # never happens. This is deliberately the LAST except clause:
            # every exception type this module already gives a more
            # specific, more useful message for is caught above and never
            # reaches here. What's left is, by construction, unanticipated
            # -- and unanticipated is exactly the case a cron entry point
            # must never turn into a bare traceback for, because there is
            # no way to enumerate every exception type every current and
            # future driver's client library might ever raise.
            #
            # CLOSE9-2: this try block now wraps `build_driver(...)` too,
            # not just `cmd_check(...)` -- the two used to be separate try
            # blocks, with only `CliError` caught around `build_driver`.
            # `build_driver`'s own documented contract is "only ever raises
            # `CliError`," but that contract living entirely in a docstring,
            # unchecked, is exactly the kind of claim this project's audits
            # have repeatedly found doesn't hold once a new driver is added
            # (blind-spot item 6). One shared try/except, with `CliError`
            # still checked FIRST so its own exit code (2) is unaffected,
            # means a future driver constructor that raises something
            # unanticipated gets the same safety net `cmd_check` itself
            # already has, instead of a second, separate gap to close later.
            print(f"error: unexpected failure evaluating provider data: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE

    if args.command == "run":
        try:
            driver = build_driver(config.provider, env=os.environ)
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
        except CliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_CONFIG_OR_SETUP_ERROR
        except (httpx.HTTPError, ProviderError) as exc:
            print(f"error: provider request failed: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE
        except ValueError as exc:
            # CLOSE7-2: see the identical `check` handler just above.
            print(f"error: could not evaluate provider data: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE
        except OSError as exc:
            # CLOSE10-2: see the identical `check` handler just above.
            print(f"error: could not record a decision: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE
        except Exception as exc:  # CLOSE8-2/CLOSE9-2, see the identical `check` handler above
            print(f"error: unexpected failure evaluating provider data: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE

    if args.command == "status":
        mailboxes = [MailboxRef(provider=config.provider, mailbox_id=m) for m in args.mailbox_id]
        return cmd_status(mailboxes=mailboxes, state_store=state_store, out=sys.stdout)

    if args.command == "resume":
        mailbox = MailboxRef(provider=config.provider, mailbox_id=args.mailbox_id)
        resumed_by = args.by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        try:
            return cmd_resume(
                mailbox=mailbox,
                state_store=state_store,
                decision_log_path=config.decision_log_path,
                resumed_by=resumed_by,
                now=datetime.now(UTC),
                out=sys.stdout,
            )
        except Exception as exc:
            # CLOSE9-2: `cmd_resume` was called bare -- no handler at all,
            # despite writing to disk via `append_resume_record` (a
            # read-only mount, a full disk, a decision-log directory owned
            # by a different user than the one running `resume` are all
            # realistic ways for that write to fail). Two things were wrong
            # with the resulting bare traceback: it contradicted `CliError`'s
            # own docstring promise, and Python's default exit code for an
            # uncaught exception is 1 -- which this project's OWN exit-code
            # map already assigns to "resume was refused." An operator
            # wrapper reading exit 1 here would conclude the resume was
            # refused (nothing to resume) when the write actually failed
            # entirely differently, and never even reached the refusal
            # check. This one broad `except Exception`, not a list of
            # specific exception types, for the same reason `check`/`run`'s
            # own catch-all (CLOSE8-2) is broad: `cmd_resume`'s own
            # docstring already enumerates what `state_store`/`from_log`
            # replaying can raise (`BreakerStateStoreLoadError`, caught
            # separately above, before `cmd_resume` is ever reached), so
            # what's left here is specifically the write path, and a write
            # can fail for reasons (`OSError` and its many subclasses --
            # `PermissionError`, and platform-specific errors this module
            # has no business enumerating) too varied to name exhaustively.
            print(f"error: could not record the resume decision: {exc}", file=sys.stderr)
            return _EXIT_PROVIDER_TRANSPORT_FAILURE

    raise AssertionError(  # pragma: no cover
        f"unreachable: argparse required a valid command, got {args.command!r}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
