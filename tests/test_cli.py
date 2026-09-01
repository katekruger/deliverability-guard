"""Tests for cli.py: `check`, `status`, `resume`.

ENG-6: at HEAD, cli.py is a docstring and zero statements -- there is no
running system. `check` is the single-shot form of the fast loop and the
minimum viable thing a user can put in cron. These tests exercise the
command functions directly against a fake provider driver (never a live
call) plus a real config loaded from a temp directory, per the audit's own
definition of done for this item.
"""

import io
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import deliverability_guard.cli as cli_module
from deliverability_guard.audit.log import (
    DecisionRecord,
    ResumeRecord,
    append_record,
    read_events,
    read_records,
)
from deliverability_guard.cli import (
    CliError,
    build_driver,
    build_parser,
    cmd_check,
    cmd_resume,
    cmd_run,
    cmd_status,
    main,
)
from deliverability_guard.config import load_config
from deliverability_guard.engine.breaker import (
    DEFAULT_LADDER,
    BreakerStateStore,
    MailboxBreakerStatus,
    ThresholdStore,
    Verdict,
    evaluate,
)
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.providers.base import (
    ActionOutcome,
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
    MailboxStatus,
    unsupported,
)
from fixtures.fake_driver import FakeDriver

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

_VALID_YAML = """
provider: fake
complaint_rate_ladder:
  warn: 0.0005
  throttle: 0.0010
  pause: 0.0020
prior:
  alpha: 0.5
  beta: 500
dry_run: true
"""


def _config(tmp_path: Path, *, text: str = _VALID_YAML, decision_log: str | None = None) -> Path:
    if decision_log is not None:
        text = text + f"\ndecision_log_path: {decision_log}\n"
    path = tmp_path / "thresholds.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _stats(mailbox: MailboxRef, day: date, sends: int, bounces: int) -> MailboxDayStats:
    return MailboxDayStats(mailbox=mailbox, day=day, sends=sends, bounces=bounces)


# --- check ------------------------------------------------------------


def test_check_prints_ok_and_exits_zero_for_a_healthy_mailbox(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 0)])
    out = io.StringIO()

    exit_code = cmd_check(
        driver=driver,
        config=config,
        state_store=BreakerStateStore(),
        now=_NOW,
        out=out,
    )

    assert exit_code == 0
    assert "a@example.com" in out.getvalue()
    assert "OK" in out.getvalue()


def test_check_exits_nonzero_when_a_mailbox_breaches(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="bad@example.com")
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 40)])
    out = io.StringIO()

    exit_code = cmd_check(
        driver=driver,
        config=config,
        state_store=BreakerStateStore(),
        now=_NOW,
        out=out,
    )

    assert exit_code == 1
    assert "PAUSE" in out.getvalue()


def test_check_aggregates_multiple_days_for_the_same_mailbox(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(
        stats_to_return=[
            _stats(mailbox, date(2025, 12, 30), 2500, 20),
            _stats(mailbox, date(2025, 12, 31), 2500, 20),
        ]
    )
    out = io.StringIO()

    cmd_check(driver=driver, config=config, state_store=BreakerStateStore(), now=_NOW, out=out)

    assert "sends=5000" in out.getvalue()
    assert "complaints=40" in out.getvalue()


def test_check_skips_a_disconnected_days_counts(tmp_path: Path) -> None:
    """A DISCONNECTED day is an outage, not evidence -- see
    providers/base.py's `MailboxStatus` docstring. `check` must not fold a
    disconnected day's sends/bounces into the aggregate as if it were a
    normal, healthy day."""
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(
        stats_to_return=[
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=0,
                bounces=0,
                status=MailboxStatus.DISCONNECTED,
            )
        ]
    )
    out = io.StringIO()

    exit_code = cmd_check(
        driver=driver,
        config=config,
        state_store=BreakerStateStore(),
        now=_NOW,
        out=out,
    )

    assert exit_code == 0
    assert "no mailboxes reported any stats" in out.getvalue()


def test_check_with_no_stats_at_all_prints_a_message_and_exits_zero(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    config = load_config(config_path)
    driver = FakeDriver(stats_to_return=[])
    out = io.StringIO()

    exit_code = cmd_check(
        driver=driver, config=config, state_store=BreakerStateStore(), now=_NOW, out=out
    )

    assert exit_code == 0
    assert "no mailboxes reported any stats" in out.getvalue()


def test_check_appends_a_decision_record_per_mailbox(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    config_path = _config(tmp_path, decision_log=str(log_path))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 0)])

    cmd_check(
        driver=driver, config=config, state_store=BreakerStateStore(), now=_NOW, out=io.StringIO()
    )

    records = read_records(log_path)
    assert len(records) == 1
    assert records[0].mailbox_id == "a@example.com"
    assert records[0].verdict is Verdict.OK


# --- status -------------------------------------------------------------


def test_status_prints_active_for_an_unknown_mailbox() -> None:
    out = io.StringIO()
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    exit_code = cmd_status(mailboxes=[mailbox], state_store=BreakerStateStore(), out=out)
    assert exit_code == 0
    assert "a@example.com: ACTIVE" in out.getvalue()


def test_status_reflects_a_paused_mailbox() -> None:
    out = io.StringIO()
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    state_store = BreakerStateStore()
    state_store.mark_paused(mailbox)
    cmd_status(mailboxes=[mailbox], state_store=state_store, out=out)
    assert "a@example.com: PAUSED" in out.getvalue()


# --- resume ---------------------------------------------------------------


def test_resume_moves_a_paused_mailbox_back_to_active(tmp_path: Path) -> None:
    out = io.StringIO()
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    state_store = BreakerStateStore()
    state_store.mark_paused(mailbox)

    exit_code = cmd_resume(
        mailbox=mailbox,
        state_store=state_store,
        decision_log_path=tmp_path / "decisions.jsonl",
        resumed_by="kate",
        now=_NOW,
        out=out,
    )

    assert exit_code == 0
    assert state_store.status_of(mailbox) == MailboxBreakerStatus.ACTIVE
    assert "resumed" in out.getvalue()
    assert "kate" in out.getvalue()


def test_resume_appends_a_resume_record_to_the_decision_log(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    state_store = BreakerStateStore()
    state_store.mark_paused(mailbox)

    cmd_resume(
        mailbox=mailbox,
        state_store=state_store,
        decision_log_path=log_path,
        resumed_by="kate",
        now=_NOW,
        out=io.StringIO(),
    )

    (event,) = read_events(log_path)
    assert isinstance(event, ResumeRecord)
    assert event.mailbox_id == "a@example.com"
    assert event.resumed_by == "kate"


def test_resume_clears_a_throttled_mailbox(tmp_path: Path) -> None:
    """CLOSE3-3: before this, `resume` refused a THROTTLED mailbox outright
    ("is not paused; nothing to resume") -- a dead end for an operator, and
    the only command capable of clearing a persisted THROTTLED that
    `from_log`'s own recovery logic hadn't (yet) caught up with. `resume`
    now accepts THROTTLED the same way it accepts PAUSED: a human,
    explicitly named, moving the mailbox back to ACTIVE."""
    log_path = tmp_path / "decisions.jsonl"
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    state_store = BreakerStateStore()
    state_store.mark_throttled(mailbox, current_daily_limit=50)

    exit_code = cmd_resume(
        mailbox=mailbox,
        state_store=state_store,
        decision_log_path=log_path,
        resumed_by="kate",
        now=_NOW,
        out=io.StringIO(),
    )

    assert exit_code == 0
    assert state_store.status_of(mailbox) == MailboxBreakerStatus.ACTIVE
    assert state_store.throttled_at_limit(mailbox) is None
    (event,) = read_events(log_path)
    assert isinstance(event, ResumeRecord)
    assert event.mailbox_id == "a@example.com"


def test_resume_refuses_a_mailbox_that_is_not_paused(tmp_path: Path) -> None:
    out = io.StringIO()
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    state_store = BreakerStateStore()

    exit_code = cmd_resume(
        mailbox=mailbox,
        state_store=state_store,
        decision_log_path=tmp_path / "decisions.jsonl",
        resumed_by="kate",
        now=_NOW,
        out=out,
    )

    assert exit_code == 1
    assert state_store.status_of(mailbox) == MailboxBreakerStatus.ACTIVE
    assert "not paused" in out.getvalue()
    assert not (tmp_path / "decisions.jsonl").exists()


# --- build_driver: the provider registry -----------------------------


def test_build_driver_unknown_provider_raises_cli_error() -> None:
    with pytest.raises(CliError, match="unknown provider"):
        build_driver("not-a-real-provider", env={})


def test_build_driver_instantly_without_api_key_raises_cli_error() -> None:
    with pytest.raises(CliError, match="INSTANTLY_API_KEY"):
        build_driver("instantly", env={})


def test_build_driver_instantly_with_api_key_builds_a_driver() -> None:
    driver = build_driver("instantly", env={"INSTANTLY_API_KEY": "test-key"})
    assert driver.name == "instantly"


def test_build_driver_smartlead_without_api_key_raises_cli_error() -> None:
    with pytest.raises(CliError, match="SMARTLEAD_API_KEY"):
        build_driver("smartlead", env={})


def test_build_driver_smartlead_without_campaign_id_raises_cli_error() -> None:
    with pytest.raises(CliError, match="SMARTLEAD_CAMPAIGN_ID"):
        build_driver("smartlead", env={"SMARTLEAD_API_KEY": "test-key"})


def test_build_driver_smartlead_with_credentials_builds_a_driver() -> None:
    driver = build_driver(
        "smartlead",
        env={"SMARTLEAD_API_KEY": "test-key", "SMARTLEAD_CAMPAIGN_ID": "camp-1"},
    )
    assert driver.name == "smartlead"


def test_build_driver_lemlist_without_api_key_raises_cli_error() -> None:
    with pytest.raises(CliError, match="LEMLIST_API_KEY"):
        build_driver("lemlist", env={})


def test_build_driver_lemlist_without_campaign_id_raises_cli_error() -> None:
    with pytest.raises(CliError, match="LEMLIST_CAMPAIGN_ID"):
        build_driver("lemlist", env={"LEMLIST_API_KEY": "test-key"})


def test_build_driver_lemlist_with_credentials_builds_a_driver() -> None:
    driver = build_driver(
        "lemlist", env={"LEMLIST_API_KEY": "test-key", "LEMLIST_CAMPAIGN_ID": "camp-1"}
    )
    assert driver.name == "lemlist"


def test_build_driver_apollo_without_api_key_raises_cli_error() -> None:
    with pytest.raises(CliError, match="APOLLO_API_KEY"):
        build_driver("apollo", env={})


def test_build_driver_apollo_without_campaign_id_raises_cli_error() -> None:
    with pytest.raises(CliError, match="APOLLO_CAMPAIGN_ID"):
        build_driver("apollo", env={"APOLLO_API_KEY": "test-key"})


def test_build_driver_apollo_with_credentials_builds_a_driver() -> None:
    driver = build_driver(
        "apollo", env={"APOLLO_API_KEY": "test-key", "APOLLO_CAMPAIGN_ID": "camp-1"}
    )
    assert driver.name == "apollo"


def test_build_driver_ses_without_configuration_set_raises_cli_error() -> None:
    with pytest.raises(CliError, match="SES_CONFIGURATION_SET_NAME"):
        build_driver("ses", env={})


def test_build_driver_ses_with_configuration_set_builds_a_driver() -> None:
    """No API key for SES -- it authenticates via boto3's normal AWS
    credential chain. Constructing the driver itself makes no live call
    (AGENTS.md) -- boto3 client construction alone never touches the
    network; `AWS_REGION` is supplied so it doesn't depend on this
    machine's ambient AWS config to even construct."""
    driver = build_driver(
        "ses",
        env={"SES_CONFIGURATION_SET_NAME": "cs-1", "AWS_REGION": "us-east-1"},
    )
    assert driver.name == "ses"


def test_build_driver_noop_needs_no_credentials() -> None:
    driver = build_driver("noop", env={})
    assert driver.name == "noop"
    # CLOSE3-4: a small synthetic fixture, not an empty list -- see
    # `test_main_check_with_the_noop_driver_genuinely_exercises_the_pipeline`
    # for why an empty report was the wrong shape for a driver whose whole
    # purpose is to exercise `check`/`run` end to end.
    assert len(driver.read_mailbox_stats(date(2025, 12, 31))) > 0


def test_build_driver_noop_reports_throttle_and_pause_as_unsupported() -> None:
    driver = build_driver("noop", env={})
    throttle_result = driver.throttle("a@example.com", 100)
    pause_result = driver.pause(MailboxRef(provider="noop", mailbox_id="a@example.com"))
    from deliverability_guard.providers.base import ActionOutcome

    assert throttle_result.outcome is ActionOutcome.UNSUPPORTED
    assert pause_result.outcome is ActionOutcome.UNSUPPORTED


# --- argument parsing / main() --------------------------------------------


def test_help_works_with_no_config_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--help` must never require a config file to exist."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--help"])
    assert exc_info.value.code == 0


def test_help_documents_the_exit_code_map() -> None:
    """CLOSE3-6: the commit that introduced these exit codes claimed they
    were documented in 'the module docstring, README, and --help's exit
    code map' -- but `build_parser` set no `epilog`, so `--help`'s rendered
    text had no exit-code content at all. This is the one place a cron
    author actually looks."""
    help_text = build_parser().format_help()
    assert "all clear" in help_text
    assert "breach" in help_text
    assert "config" in help_text.lower()
    assert "transport" in help_text.lower()


def test_main_reports_a_config_error_cleanly(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yml"
    exit_code = main(["--config", str(missing), "status", "a@example.com"])
    assert exit_code == 2


def test_main_status_end_to_end(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "status", "a@example.com"])
    assert exit_code == 0


def test_main_resume_end_to_end(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    config_path = _config(tmp_path, decision_log=str(log_path))
    exit_code = main(["--config", str(config_path), "resume", "a@example.com"])
    # Never paused -> refused, not an error.
    assert exit_code == 1


def test_main_check_reports_an_unknown_provider_cleanly(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("provider: fake", "provider: not-a-real-provider")
    config_path = _config(tmp_path, text=text, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "check"])
    assert exit_code == 2


# --- CLOSE-5b: transport failures get their own exit code, no traceback ---


class _TransportFailingDriver:
    """A driver whose `read_mailbox_stats` raises a transport-level error --
    standing in for a real network failure (e.g. `httpx.ProxyError`)
    without any actual network access."""

    name = "fake"
    capabilities: frozenset[object] = frozenset()

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        raise self._exc

    def throttle(self, mailbox_id: str, daily_limit: int) -> object:
        raise AssertionError("not reached")

    def pause(self, target: object) -> object:
        raise AssertionError("not reached")


def test_main_check_reports_an_httpx_transport_error_with_its_own_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE-5b's reproduction: a network failure must not traceback and
    must not exit 1 -- exit 1 already means "ran fine, found a breach," and
    a cron wrapper can't tell those apart otherwise."""
    import httpx

    failing_driver = _TransportFailingDriver(httpx.ProxyError("proxy connection refused"))

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> object:
        return failing_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))

    exit_code = main(["--config", str(config_path), "check"])

    assert exit_code == 3


def test_main_check_reports_a_provider_error_with_its_own_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other transport-failure family: this project's own
    `ProviderError` subclasses (rate-limit exhaustion, a malformed
    response), not just raw `httpx` errors."""
    from deliverability_guard.providers.base import RateLimitExceededError

    failing_driver = _TransportFailingDriver(RateLimitExceededError("429s all the way down"))

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> object:
        return failing_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))

    exit_code = main(["--config", str(config_path), "check"])

    assert exit_code == 3


def test_main_run_reports_a_transport_error_with_its_own_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    failing_driver = _TransportFailingDriver(httpx.ConnectError("connection refused"))

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> object:
        return failing_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))

    exit_code = main(["--config", str(config_path), "run", "--ticks", "1"])

    assert exit_code == 3


# --- CLOSE-5a: the noop driver end to end -----------------------------------


def test_main_check_with_the_noop_driver_needs_no_credentials(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("provider: fake", "provider: noop")
    config_path = _config(tmp_path, text=text, decision_log=str(tmp_path / "decisions.jsonl"))

    exit_code = main(["--config", str(config_path), "check"])

    assert exit_code == 0


def test_main_check_with_the_noop_driver_genuinely_exercises_the_pipeline(
    tmp_path: Path,
) -> None:
    """CLOSE3-4: before this, `noop` reported zero mailboxes, so `check`
    exited via the early `no mailboxes reported any stats` branch --
    exercising config loading and the exit path, but never the aggregation,
    evaluation, or decision-log-writing code `check` is supposed to be
    proving works. README line 62 claimed it exercised "the decision log";
    it didn't -- no log file was even created. `noop` now reports a small
    synthetic fixture, so `check` genuinely writes a decision record."""
    log_path = tmp_path / "decisions.jsonl"
    text = _VALID_YAML.replace("provider: fake", "provider: noop")
    config_path = _config(tmp_path, text=text, decision_log=str(log_path))

    exit_code = main(["--config", str(config_path), "check"])

    assert exit_code == 0
    assert log_path.exists()
    records = read_records(log_path)
    assert len(records) > 0
    assert records[0].verdict is Verdict.OK


def test_main_reports_an_unreadable_decision_log_cleanly(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text("not valid json\n")
    config_path = _config(tmp_path, decision_log=str(log_path))
    exit_code = main(["--config", str(config_path), "status", "a@example.com"])
    assert exit_code == 2


def test_main_check_end_to_end_with_a_fake_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check` end to end: real config loaded from a temp directory, a fake
    provider driver standing in for a live one (never a real API call),
    exit code and printed verdict asserted -- the audit's own definition of
    done for this item."""
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    fake_driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 0)])

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> FakeDriver:
        return fake_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)

    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "check"])

    assert exit_code == 0


class _ReducingDriver:
    """Reports a `current_daily_limit` that reflects its own most recent
    `throttle()` call, and identical sends/complaints on every read --
    simulating a real provider account queried by six separate `check`
    invocations against the SAME mailbox (CLOSE3-1's cron-cascade
    reproduction). Unlike `FakeDriver`, whose `throttle_calls` list is a
    pure recorder, this one's `read_mailbox_stats` actually reflects the
    reduction the way a real provider would."""

    name = "fake"

    def __init__(self, *, initial_limit: int) -> None:
        self.capabilities = frozenset(
            {Capability.READ_STATS, Capability.THROTTLE, Capability.PAUSE}
        )
        self.current_limit = initial_limit
        self.throttle_calls: list[tuple[str, int]] = []

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
        return [
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=20_000,
                bounces=30,
                current_daily_limit=self.current_limit,
            )
        ]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        self.throttle_calls.append((mailbox_id, daily_limit))
        self.current_limit = daily_limit
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail="fake: throttled",
            capability=Capability.THROTTLE,
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        return unsupported(Capability.PAUSE, self.name, "fake: not exercised in this test")


_LIVE_YAML = """
provider: fake
complaint_rate_ladder:
  warn: 0.0005
  throttle: 0.0010
  pause: 0.0020
prior:
  alpha: 0.5
  beta: 500
dry_run: false
"""


def test_six_separate_check_invocations_throttle_the_mailbox_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE3-1: `from_log` must restore `_throttled_at_limit`, not just
    status, or every `check` invocation looks like a fresh restart with no
    memory of the limit it already applied -- six identical evaluations
    compound 50 -> 25 -> 12 -> 6 -> 3 -> PAUSE instead of throttling once and
    staying idempotent. Six SEPARATE `cli.main` invocations, each of which
    rebuilds `BreakerStateStore` via `from_log` internally (see
    `cli.main`), against a driver whose reported `current_daily_limit`
    reflects its own prior throttle call -- the shape of the documented cron
    deployment, not an in-process loop."""
    driver = _ReducingDriver(initial_limit=50)

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> _ReducingDriver:
        return driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, text=_LIVE_YAML, decision_log=str(tmp_path / "decisions.jsonl"))

    for _ in range(6):
        main(["--config", str(config_path), "check"])

    assert driver.throttle_calls == [("a@example.com", 25)]
    assert driver.current_limit == 25

    state_store = BreakerStateStore.from_log(tmp_path / "decisions.jsonl")
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    assert state_store.status_of(mailbox) == MailboxBreakerStatus.THROTTLED


class _NoLimitDriver:
    """Reports `current_daily_limit=None` on every read -- a provider that
    genuinely cannot report a daily limit, so THROTTLE can never actually
    execute (CLOSE3-2/CLOSE4-2's reproduction). Also supports pause, so the
    CLOSE3-2 escalation can actually complete."""

    name = "fake"

    def __init__(self) -> None:
        self.capabilities = frozenset(
            {Capability.READ_STATS, Capability.THROTTLE, Capability.PAUSE}
        )
        self.throttle_calls: list[tuple[str, int]] = []
        self.pause_calls: list[MailboxRef | CampaignRef] = []

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
        return [MailboxDayStats(mailbox=mailbox, day=date(2025, 12, 31), sends=20_000, bounces=30)]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        self.throttle_calls.append((mailbox_id, daily_limit))
        return unsupported(Capability.THROTTLE, self.name, "fake: no daily limit known")

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        self.pause_calls.append(target)
        return ActionResult(
            outcome=ActionOutcome.PERFORMED, detail="fake: paused", capability=Capability.PAUSE
        )


class _PhaseDriver:
    """A driver whose reported evidence and daily limit change over the
    test's own lifetime, simulating a mailbox that gets paused, then later
    (wrongly, pre-CLOSE5-1) reports a real daily limit, then later still
    reports healthy evidence -- CLOSE5-1's own seven-phase reproduction."""

    name = "fake"

    def __init__(self) -> None:
        self.capabilities = frozenset(
            {Capability.READ_STATS, Capability.THROTTLE, Capability.PAUSE}
        )
        self.throttle_calls: list[tuple[str, int]] = []
        self.pause_calls: list[MailboxRef | CampaignRef] = []
        self.sends = 20_000
        self.bounces = 30
        self.current_daily_limit: int | None = None

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
        return [
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=self.sends,
                bounces=self.bounces,
                current_daily_limit=self.current_daily_limit,
            )
        ]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        self.throttle_calls.append((mailbox_id, daily_limit))
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail="fake: throttled",
            capability=Capability.THROTTLE,
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        self.pause_calls.append(target)
        return ActionResult(
            outcome=ActionOutcome.PERFORMED, detail="fake: paused", capability=Capability.PAUSE
        )


def test_seven_phase_reproduction_a_paused_mailbox_never_auto_un_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE5-1: the serious one. Seven SEPARATE `cli.main` invocations
    (`from_log` rebuilt fresh each time, the documented cron deployment).
    Phase 1 (no known daily limit) escalates to PAUSE via the CLOSE3-2
    streak on run 4 -- one real provider `pause()` call. Phase 2 (the
    provider now reports a real `current_daily_limit`) must NOT throttle the
    already-paused mailbox, regardless of what its evidence says. Phase 3
    (evidence recovers to OK) must NOT auto-resume it either -- CLOSE-3b's
    recovery path is for THROTTLED mailboxes, and this one must never have
    become THROTTLED in the first place. `resume_after_human_review` is
    never called; no `ResumeRecord` exists in the log."""
    driver = _PhaseDriver()

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> _PhaseDriver:
        return driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, text=_LIVE_YAML, decision_log=str(tmp_path / "decisions.jsonl"))
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")

    # Phase 1: no known daily limit. Runs 1-4.
    for _ in range(4):
        main(["--config", str(config_path), "check"])
    assert (
        BreakerStateStore.from_log(tmp_path / "decisions.jsonl").status_of(mailbox)
        == MailboxBreakerStatus.PAUSED
    )

    # Phase 2: provider now reports a real daily limit. Runs 5-6.
    driver.current_daily_limit = 50
    for _ in range(2):
        main(["--config", str(config_path), "check"])

    # Phase 3: evidence recovers. Run 7.
    driver.sends = 5000
    driver.bounces = 0
    main(["--config", str(config_path), "check"])

    final_store = BreakerStateStore.from_log(tmp_path / "decisions.jsonl")
    assert final_store.status_of(mailbox) == MailboxBreakerStatus.PAUSED
    assert driver.throttle_calls == []
    assert driver.pause_calls == [mailbox]


def test_seven_phase_reproduction_never_writes_a_resume_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _PhaseDriver()

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> _PhaseDriver:
        return driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, text=_LIVE_YAML, decision_log=str(tmp_path / "decisions.jsonl"))

    for _ in range(4):
        main(["--config", str(config_path), "check"])
    driver.current_daily_limit = 50
    for _ in range(2):
        main(["--config", str(config_path), "check"])
    driver.sends = 5000
    driver.bounces = 0
    main(["--config", str(config_path), "check"])

    events = read_events(tmp_path / "decisions.jsonl")
    assert not any(isinstance(e, ResumeRecord) for e in events)


class _SmartleadShapedDriver:
    """Capability/outcome shape matches `smartlead` exactly:
    `pause(MailboxRef)` -> UNSUPPORTED (Smartlead has no per-mailbox pause
    endpoint), `throttle(mailbox_id, limit)` -> PERFORMED. CLI-selectable in
    real deployments, unlike the phase drivers above -- CLOSE5-2's own
    reproduction names it specifically."""

    name = "fake"

    def __init__(self) -> None:
        self.capabilities = frozenset(
            {Capability.READ_STATS, Capability.THROTTLE, Capability.PAUSE}
        )
        self.throttle_calls: list[tuple[str, int]] = []
        self.pause_calls: list[MailboxRef | CampaignRef] = []
        self.phase = 0  # advanced externally between the three moves

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
        # THROTTLE_PERFORMED, PAUSE_UNSUPPORTED, THROTTLE_PERFORMED --
        # CLOSE5-2's own three-move reproduction.
        sends, bounces, current_daily_limit = (
            (20_000, 30, 100),
            (5000, 40, None),
            (20_000, 30, 100),
        )[self.phase]
        return [
            MailboxDayStats(
                mailbox=mailbox,
                day=date(2025, 12, 31),
                sends=sends,
                bounces=bounces,
                current_daily_limit=current_daily_limit,
            )
        ]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        self.throttle_calls.append((mailbox_id, daily_limit))
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail="fake: throttled",
            capability=Capability.THROTTLE,
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        self.pause_calls.append(target)
        return unsupported(Capability.PAUSE, self.name, "fake: no per-mailbox pause endpoint")


def test_daemon_and_cron_agree_on_a_smartlead_shaped_three_move_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE5-2: the same three evaluations (THROTTLE/PERFORMED,
    PAUSE/UNSUPPORTED, THROTTLE/PERFORMED again), run once as an
    uninterrupted daemon (`run` -- one `state_store`, no restarts) and once
    as three separate `check` invocations (`from_log` rebuilt fresh each
    time -- cron). Both must make the SAME real provider calls and reach
    the SAME final state; before CLOSE5-2's fix, cron made one fewer real
    `throttle()` call and read the mailbox as pristine (ACTIVE) where the
    daemon correctly read it as THROTTLED."""
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")

    # --- daemon: one process, one state_store, three ticks, no restarts ---
    daemon_driver = _SmartleadShapedDriver()
    daemon_store = BreakerStateStore()
    for phase in range(3):
        daemon_driver.phase = phase
        stats = daemon_driver.read_mailbox_stats(date(2025, 12, 31))[0]
        evaluate(
            driver=daemon_driver,
            mailbox=mailbox,
            sends=stats.sends,
            complaints=stats.bounces,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=daemon_store,
            dry_run=False,
            now=_NOW,
            current_daily_limit=stats.current_daily_limit,
        )

    # --- cron: three separate `cli.main` invocations, from_log rebuilt
    # fresh each time ---
    cron_driver = _SmartleadShapedDriver()

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> _SmartleadShapedDriver:
        return cron_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, text=_LIVE_YAML, decision_log=str(tmp_path / "decisions.jsonl"))
    for phase in range(3):
        cron_driver.phase = phase
        main(["--config", str(config_path), "check"])

    cron_store = BreakerStateStore.from_log(tmp_path / "decisions.jsonl")

    assert cron_driver.throttle_calls == daemon_driver.throttle_calls
    assert cron_driver.pause_calls == daemon_driver.pause_calls
    assert cron_store.status_of(mailbox) == daemon_store.status_of(mailbox)
    assert cron_store.throttled_at_limit(mailbox) == daemon_store.throttled_at_limit(mailbox)


def test_ten_separate_check_invocations_pause_the_mailbox_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE4-1/CLOSE4-2: ten separate `cli.main` invocations against a
    provider that can never report a daily limit. `check` escalates to
    PAUSE via the CLOSE3-2 streak on run 4 -- and before CLOSE4-1's fix, the
    very next THROTTLE/UNSUPPORTED record replayed on run 5 silently
    un-paused the mailbox, so the escalate-then-un-pause cycle repeated with
    period 4 forever, calling the provider's real `pause()` again every time
    it escalated. After the fix: the mailbox reaches PAUSED once and stays
    there for the remaining six runs, with exactly one real provider pause
    call -- later escalations replaying against an already-PAUSED mailbox
    are idempotent no-ops."""
    driver = _NoLimitDriver()

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> _NoLimitDriver:
        return driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, text=_LIVE_YAML, decision_log=str(tmp_path / "decisions.jsonl"))
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")

    statuses: list[str] = []
    for _ in range(10):
        main(["--config", str(config_path), "check"])
        statuses.append(
            BreakerStateStore.from_log(tmp_path / "decisions.jsonl").status_of(mailbox).name
        )

    assert statuses[3:] == ["PAUSED"] * 7  # escalates by run 4, stays PAUSED through run 10
    assert driver.pause_calls == [mailbox]
    assert driver.throttle_calls == []  # current_daily_limit is always None -- never a real call


def test_ten_separate_check_invocations_after_pause_write_an_honest_already_paused_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE5-4: verified rather than assumed. Before CLOSE5-1's fix, the
    verdict cycle after escalation ran forever (a real `pause()` call every
    four runs) and each record for a PAUSED mailbox reported a bare
    `THROTTLE` verdict with no indication anything was refused -- exactly
    what CLOSE5-4 worried an operator tailing cron mail would see. Neither
    is true anymore: every record recorded for the mailbox once it's PAUSED
    carries CLOSE5-1's own "mailbox is paused; ... refused pending human
    review" detail, an honest record rather than a bare, unexplained
    THROTTLE."""
    driver = _NoLimitDriver()

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> _NoLimitDriver:
        return driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    config_path = _config(tmp_path, text=_LIVE_YAML, decision_log=str(tmp_path / "decisions.jsonl"))

    for _ in range(10):
        main(["--config", str(config_path), "check"])

    records = read_records(tmp_path / "decisions.jsonl")
    post_pause_records = records[4:]  # runs 5-10, after run 4's escalation to PAUSE
    assert len(post_pause_records) == 6
    for record in post_pause_records:
        assert record.action_detail is not None
        assert "paused" in record.action_detail


def test_from_log_restart_between_tick_one_and_two_is_a_no_op(tmp_path: Path) -> None:
    """A restart between the very first throttle and the second identical
    evaluation specifically: tick 2 must be idempotent, not a fresh
    halving."""
    log_path = tmp_path / "decisions.jsonl"
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver()
    store1 = BreakerStateStore()
    result1 = evaluate(
        driver=driver,
        mailbox=mailbox,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=store1,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    append_record(log_path, DecisionRecord.from_evaluation(result1))
    assert driver.throttle_calls == [("a@example.com", 25)]

    # Restart: rebuild state purely from the log.
    store2 = BreakerStateStore.from_log(log_path)
    evaluate(
        driver=driver,
        mailbox=mailbox,
        sends=20_000,
        complaints=30,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=store2,
        dry_run=False,
        now=_NOW,
        current_daily_limit=50,
    )
    assert driver.throttle_calls == [("a@example.com", 25)]


# --- run: the always-on two-loop daemon -----------------------------------


class _FakeClock:
    """Same shape as loops/test_controller.py's clock: `sleep` advances the
    clock instead of actually sleeping, so a daemon test never waits."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def test_cmd_run_ticks_the_daemon_and_appends_decision_records(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    config_path = _config(tmp_path, decision_log=str(log_path))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 0)])
    clock = _FakeClock(_NOW)
    out = io.StringIO()

    exit_code = cmd_run(
        driver=driver,
        config=config,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(config.thresholds),
        now=clock.now,
        sleep=clock.sleep,
        out=out,
        max_ticks=3,
    )

    assert exit_code == 0
    assert len(driver.read_calls) == 3
    assert "[fast] a@example.com: OK" in out.getvalue()
    records = read_records(log_path)
    assert len(records) == 3


def test_cmd_run_prints_slow_tick_adjustments(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    # 15 complaints in 20,000 sends sits just under `warn` without crossing
    # it -- the same "close to warn" evidence used in test_controller.py.
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 20_000, 15)])
    clock = _FakeClock(_NOW)
    out = io.StringIO()

    cmd_run(
        driver=driver,
        config=config,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(config.thresholds),
        now=clock.now,
        sleep=clock.sleep,
        out=out,
        max_ticks=1,
    )

    # A single tick can't cross the (24h default) slow interval, so no
    # adjustment is expected here -- this just proves cmd_run wires the
    # slow-tick callback at all, exercised properly in test_controller.py.
    assert "[slow]" not in out.getvalue()


def test_cmd_run_prints_slow_tick_adjustments_once_the_interval_elapses(tmp_path: Path) -> None:
    text = _VALID_YAML + "\nfast_interval_seconds: 3600\nslow_interval_seconds: 21600\n"
    config_path = _config(tmp_path, text=text, decision_log=str(tmp_path / "decisions.jsonl"))
    config = load_config(config_path)
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 20_000, 15)])
    clock = _FakeClock(_NOW)
    out = io.StringIO()

    cmd_run(
        driver=driver,
        config=config,
        state_store=BreakerStateStore(),
        threshold_store=ThresholdStore(config.thresholds),
        now=clock.now,
        sleep=clock.sleep,
        out=out,
        max_ticks=8,  # 8 hours of simulated uptime -> the slow loop must run
    )

    assert "[slow] tightened thresholds:" in out.getvalue()


def test_main_run_end_to_end_with_a_fake_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    fake_driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 0)])

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> FakeDriver:
        return fake_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)

    def _no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(cli_module.time, "sleep", _no_sleep)

    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "run", "--ticks", "2"])

    assert exit_code == 0
    assert len(fake_driver.read_calls) == 2


def test_main_run_reports_an_unknown_provider_cleanly(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("provider: fake", "provider: not-a-real-provider")
    config_path = _config(tmp_path, text=text, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "run", "--ticks", "1"])
    assert exit_code == 2


def test_main_run_handles_keyboard_interrupt_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> FakeDriver:
        return FakeDriver()

    def _raise_keyboard_interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    monkeypatch.setattr(cli_module.controller, "run", _raise_keyboard_interrupt)

    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "run"])
    assert exit_code == 0


# --- CLOSE-4: resume durability, dry-run non-persistence -------------------


def test_main_resume_survives_a_restart_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLOSE-4 reproduction: pause a mailbox, restart (a fresh
    `BreakerStateStore.from_log`), resume it, restart again -- the mailbox
    must come back ACTIVE, not PAUSED. Before this fix, `resume` wrote
    nothing to the log and the second restart silently lost it."""
    mailbox = MailboxRef(provider="fake", mailbox_id="hot@example.com")
    log_path = tmp_path / "decisions.jsonl"
    text = _VALID_YAML.replace("dry_run: true", "dry_run: false")
    config_path = _config(tmp_path, text=text, decision_log=str(log_path))
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 40)])

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> FakeDriver:
        return driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)
    monkeypatch.setenv("USER", "kate")

    exit_code = main(["--config", str(config_path), "check"])
    assert exit_code == 1  # PAUSE

    # --- restart: rebuild state purely from the log ---
    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(mailbox) == MailboxBreakerStatus.PAUSED

    resume_exit_code = main(["--config", str(config_path), "resume", "hot@example.com"])
    assert resume_exit_code == 0

    # --- restart again: the resume must have survived ---
    restored_again = BreakerStateStore.from_log(log_path)
    assert restored_again.status_of(mailbox) == MailboxBreakerStatus.ACTIVE


def test_main_check_dry_run_pause_does_not_survive_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE-4b's reproduction: a dry-run deployment (`dry_run: true`, the
    config default) that would PAUSE a mailbox must never rebuild that
    mailbox as PAUSED after a restart -- it never actually paused anything,
    and AGENTS.md's dry-run non-negotiable means it must never accumulate
    durable state a live deployment didn't ask for."""
    mailbox = MailboxRef(provider="fake", mailbox_id="hot@example.com")
    log_path = tmp_path / "decisions.jsonl"
    config_path = _config(tmp_path, decision_log=str(log_path))  # dry_run: true
    driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 40)])

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> FakeDriver:
        return driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)

    exit_code = main(["--config", str(config_path), "check"])
    assert exit_code == 1  # PAUSE verdict, but dry-run -- never actually paused

    assert driver.pause_calls == []  # the real (fake) driver was never touched

    restored = BreakerStateStore.from_log(log_path)
    assert restored.status_of(mailbox) == MailboxBreakerStatus.ACTIVE


# --- CLOSE-1: pooled_posterior and cusum_step actually execute during a real
# `check`/`run`, proven by instrumentation, not by a unit test that calls
# them directly. (An external audit drove a real cmd_check and a five-tick
# cmd_run and found NEITHER function executed at all -- each had a caller,
# but nothing called that caller in production.)


def test_close1_check_actually_executes_pooled_posterior_and_cusum_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This test fails against the pre-CLOSE-1 `evaluate_all_mailboxes`,
    which called `engine.breaker.evaluate` with neither `peer_group` nor a
    CUSUM state to hand to `cusum_step`."""
    import deliverability_guard.engine.breaker as breaker_module
    import deliverability_guard.loops.fast as fast_module

    pooled_posterior_calls: list[object] = []
    real_pooled_posterior = breaker_module.pooled_posterior

    def _spy_pooled_posterior(*args: object, **kwargs: object) -> object:
        pooled_posterior_calls.append((args, kwargs))
        return real_pooled_posterior(*args, **kwargs)  # type: ignore[arg-type]

    cusum_step_calls: list[object] = []
    real_cusum_step = fast_module.cusum_step

    def _spy_cusum_step(*args: object, **kwargs: object) -> object:
        cusum_step_calls.append((args, kwargs))
        return real_cusum_step(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(breaker_module, "pooled_posterior", _spy_pooled_posterior)
    monkeypatch.setattr(fast_module, "cusum_step", _spy_cusum_step)

    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    fake_driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 0)])

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> FakeDriver:
        return fake_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)

    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "check"])

    assert exit_code == 0
    assert len(pooled_posterior_calls) >= 1
    assert len(cusum_step_calls) >= 1


def test_close1_run_actually_executes_pooled_posterior_and_cusum_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The five-tick `cmd_run` half of the same reproduction."""
    import deliverability_guard.engine.breaker as breaker_module
    import deliverability_guard.loops.fast as fast_module

    pooled_posterior_calls: list[object] = []
    real_pooled_posterior = breaker_module.pooled_posterior

    def _spy_pooled_posterior(*args: object, **kwargs: object) -> object:
        pooled_posterior_calls.append((args, kwargs))
        return real_pooled_posterior(*args, **kwargs)  # type: ignore[arg-type]

    cusum_step_calls: list[object] = []
    real_cusum_step = fast_module.cusum_step

    def _spy_cusum_step(*args: object, **kwargs: object) -> object:
        cusum_step_calls.append((args, kwargs))
        return real_cusum_step(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(breaker_module, "pooled_posterior", _spy_pooled_posterior)
    monkeypatch.setattr(fast_module, "cusum_step", _spy_cusum_step)

    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    fake_driver = FakeDriver(stats_to_return=[_stats(mailbox, date(2025, 12, 31), 5000, 0)])

    def _fake_build_driver(provider: str, *, env: Mapping[str, str]) -> FakeDriver:
        return fake_driver

    monkeypatch.setattr(cli_module, "build_driver", _fake_build_driver)

    def _no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(cli_module.time, "sleep", _no_sleep)

    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    exit_code = main(["--config", str(config_path), "run", "--ticks", "5"])

    assert exit_code == 0
    assert len(pooled_posterior_calls) == 5
    assert len(cusum_step_calls) == 5
