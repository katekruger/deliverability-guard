"""Tests for mcp_server.py -- deliverability-guard's read-only MCP surface
(BUILD-PLAN.md §4 item #27). Tool functions are tested as plain Python
functions (no MCP client, no protocol layer); `build_server` is exercised
through the MCP SDK's own in-process `list_tools`/`call_tool` to confirm
registration actually wires up correctly, still with no network of any
kind."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

import deliverability_guard.mcp_server as mcp_server_module
from deliverability_guard.audit.log import DecisionRecord, append_record
from deliverability_guard.engine.breaker import DEFAULT_LADDER, BreakerStateStore, evaluate
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.mcp_server import (
    build_server,
    get_mailbox_status,
    get_thresholds,
    list_recent_decisions,
    main,
)
from deliverability_guard.providers.base import MailboxRef
from fixtures.fake_driver import FakeDriver

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


def _config(tmp_path: Path, *, decision_log: str) -> Path:
    path = tmp_path / "thresholds.yml"
    path.write_text(_VALID_YAML + f"\ndecision_log_path: {decision_log}\n", encoding="utf-8")
    return path


# --- get_mailbox_status ----------------------------------------------------


def test_get_mailbox_status_for_an_unknown_mailbox_is_active(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    assert get_mailbox_status(config_path, "a@example.com") == "ACTIVE"


def test_get_mailbox_status_reflects_a_paused_mailbox(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    config_path = _config(tmp_path, decision_log=str(log_path))
    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    driver = FakeDriver()
    evaluation = evaluate(
        driver=driver,
        mailbox=mailbox,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    assert get_mailbox_status(config_path, "a@example.com") == "PAUSED"


# --- get_thresholds ---------------------------------------------------------


def test_get_thresholds_returns_the_configured_ladder(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    assert get_thresholds(config_path) == {"warn": 0.0005, "throttle": 0.0010, "pause": 0.0020}


def test_get_thresholds_reflects_a_custom_ladder(tmp_path: Path) -> None:
    text = _VALID_YAML.replace("warn: 0.0005", "warn: 0.0001").replace(
        "decision_log_path", "decision_log_path"
    )
    path = tmp_path / "thresholds.yml"
    path.write_text(text + f"\ndecision_log_path: {tmp_path / 'decisions.jsonl'}\n")
    assert get_thresholds(path)["warn"] == 0.0001


# --- list_recent_decisions ---------------------------------------------------


def test_list_recent_decisions_with_no_log_is_empty(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    assert list_recent_decisions(config_path, "a@example.com") == []


def test_list_recent_decisions_filters_to_the_requested_mailbox(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    config_path = _config(tmp_path, decision_log=str(log_path))
    driver = FakeDriver()

    for mailbox_id in ("a@example.com", "b@example.com"):
        evaluation = evaluate(
            driver=driver,
            mailbox=MailboxRef(provider="fake", mailbox_id=mailbox_id),
            sends=5000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    records = list_recent_decisions(config_path, "a@example.com")
    assert len(records) == 1
    assert records[0]["mailbox_id"] == "a@example.com"


def test_list_recent_decisions_is_newest_first_and_respects_limit(tmp_path: Path) -> None:
    log_path = tmp_path / "decisions.jsonl"
    config_path = _config(tmp_path, decision_log=str(log_path))
    driver = FakeDriver()

    mailbox = MailboxRef(provider="fake", mailbox_id="a@example.com")
    for day in (1, 2, 3):
        evaluation = evaluate(
            driver=driver,
            mailbox=mailbox,
            sends=5000,
            complaints=0,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=datetime(2026, 1, day, tzinfo=UTC),
        )
        append_record(log_path, DecisionRecord.from_evaluation(evaluation))

    records = list_recent_decisions(config_path, "a@example.com", limit=2)
    assert len(records) == 2
    assert str(records[0]["evaluated_at"]).startswith("2026-01-03")
    assert str(records[1]["evaluated_at"]).startswith("2026-01-02")


def test_list_recent_decisions_rejects_nonpositive_limit(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    with pytest.raises(ValueError, match="limit"):
        list_recent_decisions(config_path, "a@example.com", limit=0)


# --- build_server: registration wiring, via the MCP SDK's own in-process --
# --- list_tools/call_tool -- no network, no live client -------------------


def test_build_server_registers_the_three_read_only_tools(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    server = build_server(config_path)

    tools = asyncio.run(server.list_tools())
    tool_names = {tool.name for tool in tools}

    assert tool_names == {"mailbox_status", "thresholds", "recent_decisions"}


def test_build_server_never_registers_a_pause_or_resume_tool(tmp_path: Path) -> None:
    """The core safety property this module exists to preserve: nothing
    that can move a mailbox out of (or into) PAUSED is ever reachable
    through this server."""
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    server = build_server(config_path)

    tools = asyncio.run(server.list_tools())
    tool_names = {tool.name.lower() for tool in tools}

    for forbidden in ("pause", "resume", "throttle"):
        assert not any(forbidden in name for name in tool_names)


def _call_tool(server: MCPServer, name: str, arguments: dict[str, object]) -> CallToolResult:
    result = asyncio.run(server.call_tool(name, arguments))
    assert isinstance(result, CallToolResult), f"expected a completed result, got {result!r}"
    return result


def test_build_server_mailbox_status_tool_calls_through_correctly(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    server = build_server(config_path)

    result = _call_tool(server, "mailbox_status", {"mailbox_id": "a@example.com"})

    assert result.is_error is False
    assert result.structured_content == {"result": "ACTIVE"}


def test_build_server_recent_decisions_tool_calls_through_correctly(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    server = build_server(config_path)

    result = _call_tool(server, "recent_decisions", {"mailbox_id": "a@example.com", "limit": 5})

    assert result.is_error is False
    assert result.structured_content == {"result": []}


def test_build_server_thresholds_tool_calls_through_correctly(tmp_path: Path) -> None:
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    server = build_server(config_path)

    result = _call_tool(server, "thresholds", {})

    assert result.is_error is False
    assert result.structured_content == {
        "warn": 0.0005,
        "throttle": 0.0010,
        "pause": 0.0020,
    }


# --- main(): the deliverability-guard-mcp console script (CLOSE3-5) -------


def test_main_builds_a_server_from_the_given_config_and_runs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLOSE3-5: before this, `build_server` was real and tested but had no
    caller outside its own test file -- no `main`, no console script.
    `main` must build from the CLI-supplied `--config` path and run the
    resulting server over stdio (never actually invoked here -- this
    monkeypatches `run` to a no-op so the test can't block)."""
    config_path = _config(tmp_path, decision_log=str(tmp_path / "decisions.jsonl"))
    built_with: list[Path] = []
    ran: list[bool] = []

    class _FakeServer:
        def run(self) -> None:
            ran.append(True)

    def _fake_build_server(config_path_arg: Path) -> _FakeServer:
        built_with.append(config_path_arg)
        return _FakeServer()

    monkeypatch.setattr(mcp_server_module, "build_server", _fake_build_server)

    exit_code = main(["--config", str(config_path)])

    assert exit_code == 0
    assert built_with == [config_path]
    assert ran == [True]


def test_main_defaults_to_the_standard_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    class _FakeServer:
        def run(self) -> None:
            pass

    def _fake_build_server(config_path_arg: Path) -> _FakeServer:
        seen.append(config_path_arg)
        return _FakeServer()

    monkeypatch.setattr(mcp_server_module, "build_server", _fake_build_server)

    main([])

    assert seen == [Path("config/thresholds.yml")]
