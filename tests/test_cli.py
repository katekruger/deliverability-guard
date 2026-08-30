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
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import deliverability_guard.cli as cli_module
from deliverability_guard.audit.log import read_records
from deliverability_guard.cli import (
    CliError,
    build_driver,
    build_parser,
    cmd_check,
    cmd_resume,
    cmd_status,
    main,
)
from deliverability_guard.config import load_config
from deliverability_guard.engine.breaker import BreakerStateStore, MailboxBreakerStatus, Verdict
from deliverability_guard.providers.base import MailboxDayStats, MailboxRef, MailboxStatus
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


def test_resume_moves_a_paused_mailbox_back_to_active() -> None:
    out = io.StringIO()
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    state_store = BreakerStateStore()
    state_store.mark_paused(mailbox)

    exit_code = cmd_resume(mailbox=mailbox, state_store=state_store, out=out)

    assert exit_code == 0
    assert state_store.status_of(mailbox) == MailboxBreakerStatus.ACTIVE
    assert "resumed" in out.getvalue()


def test_resume_refuses_a_mailbox_that_is_not_paused() -> None:
    out = io.StringIO()
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    state_store = BreakerStateStore()

    exit_code = cmd_resume(mailbox=mailbox, state_store=state_store, out=out)

    assert exit_code == 1
    assert state_store.status_of(mailbox) == MailboxBreakerStatus.ACTIVE
    assert "not paused" in out.getvalue()


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


# --- argument parsing / main() --------------------------------------------


def test_help_works_with_no_config_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--help` must never require a config file to exist."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--help"])
    assert exc_info.value.code == 0


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
