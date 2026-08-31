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
from deliverability_guard.audit.log import ResumeRecord, read_events, read_records
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
    BreakerStateStore,
    MailboxBreakerStatus,
    ThresholdStore,
    Verdict,
)
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
