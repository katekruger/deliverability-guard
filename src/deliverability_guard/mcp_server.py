"""MCP server wrapping deliverability-guard's READ surface (BUILD-PLAN.md
§4 item #27: "Ties into the rest of the portfolio").

Deliberately read-only. This module exposes breaker state, configured
thresholds, and recent decision-log entries as MCP tools -- never
`resume`, `pause`, or `throttle`. ADR 0003's entire guarantee is that a
paused mailbox only ever comes back via an explicit, typed human action
(`cli.cmd_resume`); wiring `resume_after_human_review` up as an
LLM-callable tool would hand that decision to whatever is on the other
end of the MCP connection, which is exactly the automatic-resume path ADR
0003 exists to rule out. If a write surface is ever wanted here, that is
a new decision requiring its own ADR, not an extension of this module.

Each tool function below (`get_mailbox_status`, `get_thresholds`,
`list_recent_decisions`) takes `config_path` as a plain, explicit
argument and contains no MCP-specific code at all -- `build_server` is the
only place that touches the `mcp` SDK, wiring each function to a
thin closure with `config_path` already bound. This keeps the actual
logic testable as ordinary Python functions, without needing a live MCP
client or the protocol layer at all.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from deliverability_guard.audit.log import read_records
from deliverability_guard.config import load_config
from deliverability_guard.engine.breaker import BreakerStateStore
from deliverability_guard.providers.base import MailboxRef

_SERVER_NAME = "deliverability-guard"
_DEFAULT_CONFIG_PATH = Path("config/thresholds.yml")


def get_mailbox_status(config_path: Path, mailbox_id: str) -> str:
    """The mailbox's current breaker state (`ACTIVE`, `THROTTLED`,
    `PAUSE_IN_FLIGHT`, or `PAUSED`), rebuilt from the decision log the same
    way `cli.cmd_status` does. An unknown mailbox correctly returns
    `ACTIVE` -- see `BreakerStateStore.status_of`.
    """
    config = load_config(config_path)
    state_store = BreakerStateStore.from_log(config.decision_log_path)
    mailbox = MailboxRef(provider=config.provider, mailbox_id=mailbox_id)
    return state_store.status_of(mailbox).name


def get_thresholds(config_path: Path) -> dict[str, float]:
    """The currently configured warn/throttle/pause ladder."""
    config = load_config(config_path)
    return {
        "warn": config.thresholds.warn,
        "throttle": config.thresholds.throttle,
        "pause": config.thresholds.pause,
    }


def list_recent_decisions(
    config_path: Path, mailbox_id: str, limit: int = 10
) -> list[dict[str, object]]:
    """The most recent `limit` decision-log records for one mailbox,
    newest first. An empty list means no decision has ever been recorded
    for this mailbox -- not the same claim as "this mailbox is healthy,"
    just that nothing has been logged about it yet.
    """
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")
    config = load_config(config_path)
    if not config.decision_log_path.exists():
        return []
    records = [
        record
        for record in read_records(config.decision_log_path)
        if record.mailbox_id == mailbox_id
    ]
    records.sort(key=lambda record: record.evaluated_at, reverse=True)
    return [record.to_dict() for record in records[:limit]]


def build_server(config_path: Path) -> MCPServer:
    """Build the MCP server, with `config_path` bound into every tool.
    Never called in tests beyond confirming registration -- the actual
    logic each tool wraps is `get_mailbox_status`/`get_thresholds`/
    `list_recent_decisions` above, tested directly.
    """
    server: MCPServer = MCPServer(_SERVER_NAME)

    def mailbox_status(mailbox_id: str) -> str:
        """Current breaker state for one mailbox (ACTIVE, THROTTLED,
        PAUSE_IN_FLIGHT, or PAUSED)."""
        return get_mailbox_status(config_path, mailbox_id)

    def thresholds() -> dict[str, float]:
        """The currently configured warn/throttle/pause threshold ladder."""
        return get_thresholds(config_path)

    def recent_decisions(mailbox_id: str, limit: int = 10) -> list[dict[str, object]]:
        """The most recent decision-log records for one mailbox, newest
        first."""
        return list_recent_decisions(config_path, mailbox_id, limit)

    # `add_tool` rather than the `@server.tool()` decorator: each function
    # above is a plain, ordinary def -- using it as a normal value here
    # (rather than only as a decorator's side effect) is what makes it
    # visibly "used" to a type checker and a human reader alike.
    server.add_tool(mailbox_status)
    server.add_tool(thresholds)
    server.add_tool(recent_decisions)

    return server


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover -- stdio server, no exit
    """Entry point for the `deliverability-guard-mcp` console script
    (CLOSE3-5: this module previously had no `main`, no script registration,
    and no caller at all outside its own test file -- `build_server` was
    real, tested, and completely unreachable). Runs over stdio, the same
    transport every other MCP server in this ecosystem defaults to; there is
    no live-call risk in importing or constructing this (AGENTS.md), only in
    actually running it against a real config.
    """
    parser = argparse.ArgumentParser(
        prog="deliverability-guard-mcp",
        description="MCP server wrapping deliverability-guard's read-only surface.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG_PATH,
        help=f"path to the thresholds config (default: {_DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args(argv)
    server = build_server(args.config)
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
