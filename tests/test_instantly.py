"""Tests for providers/instantly.py, against recorded fixtures only -- no live calls."""

from datetime import date

import httpx
import pytest

from deliverability_guard.providers.base import (
    ActionOutcome,
    CampaignRef,
    Capability,
    MailboxRef,
    MailboxStatus,
    MalformedResponseError,
    RateLimitExceededError,
)
from deliverability_guard.providers.instantly import InstantlyDriver
from fixtures.http import load_json, queued_client, response


def _driver(client: httpx.Client) -> InstantlyDriver:
    return InstantlyDriver(api_key="test-key", client=client)


def test_read_mailbox_stats_parses_a_normal_response() -> None:
    client = queued_client(
        [response(200, "instantly/analytics_daily_200.json")],
        base_url="https://api.instantly.ai",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1))
    by_email = {s.mailbox.mailbox_id: s for s in stats}

    assert by_email["sender1@example.com"].sends == 50
    assert by_email["sender1@example.com"].bounces == 1
    assert by_email["sender1@example.com"].status == MailboxStatus.ACTIVE
    assert by_email["sender1@example.com"].day == date(2026, 8, 1)


def test_read_mailbox_stats_flags_a_disconnected_mailbox() -> None:
    """Mailbox disconnected -> outage, not a reputation breach. The driver's
    job is only to surface the status; interpreting it is the breaker's."""
    client = queued_client(
        [response(200, "instantly/analytics_daily_200.json")],
        base_url="https://api.instantly.ai",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1))
    by_email = {s.mailbox.mailbox_id: s for s in stats}
    assert by_email["sender2@example.com"].status == MailboxStatus.DISCONNECTED


def test_read_mailbox_stats_normalizes_a_timezone_offset_to_utc() -> None:
    """23:30 in UTC-7 is 06:30 the NEXT day in UTC -- a real off-by-one-day
    bug if a driver doesn't normalize before aggregating."""
    client = queued_client(
        [response(200, "instantly/analytics_daily_200.json")],
        base_url="https://api.instantly.ai",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1))
    by_email = {s.mailbox.mailbox_id: s for s in stats}
    entry = by_email["sender3@example.com"]
    assert entry.day == date(2026, 8, 2)
    assert entry.source_day == "2026-08-01T23:30:00-07:00"


def test_read_mailbox_stats_auto_registers_an_unseen_mailbox() -> None:
    """Provider returns a mailbox we've never seen -> auto-register, don't
    crash. There's no allowlist here to begin with -- every row just
    becomes a MailboxRef -- so this is really a non-crash guarantee."""
    client = queued_client(
        [response(200, "instantly/analytics_daily_200.json")],
        base_url="https://api.instantly.ai",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1))
    assert {s.mailbox.mailbox_id for s in stats} == {
        "sender1@example.com",
        "sender2@example.com",
        "sender3@example.com",
    }


def test_read_mailbox_stats_raises_on_malformed_response() -> None:
    client = queued_client(
        [response(200, "instantly/analytics_daily_malformed.json")],
        base_url="https://api.instantly.ai",
    )
    with pytest.raises(MalformedResponseError):
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1))


def test_read_mailbox_stats_raises_when_status_is_an_error() -> None:
    client = queued_client(
        [httpx.Response(500, json={"error": "internal"})],
        base_url="https://api.instantly.ai",
    )
    with pytest.raises(MalformedResponseError, match="500"):
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1))


def test_read_mailbox_stats_raises_on_invalid_json_body() -> None:
    client = queued_client(
        [httpx.Response(200, content=b"not json")],
        base_url="https://api.instantly.ai",
    )
    with pytest.raises(MalformedResponseError, match="JSON"):
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1))


def test_read_mailbox_stats_accepts_a_bare_list_response() -> None:
    """Not every provider wraps rows in a {"data": [...]} envelope --
    accept a bare list too, per _extract_rows's documented contract."""
    client = queued_client(
        [
            httpx.Response(
                200,
                json=[{"email": "a@example.com", "date": "2026-08-01", "sent": 5, "bounced": 0}],
            )
        ],
        base_url="https://api.instantly.ai",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1))
    assert len(stats) == 1


def test_read_mailbox_stats_retries_past_429_then_succeeds() -> None:
    client = queued_client(
        [
            response(429, "instantly/rate_limited_429.json"),
            response(200, "instantly/analytics_daily_200.json"),
        ],
        base_url="https://api.instantly.ai",
    )
    driver = InstantlyDriver(api_key="test-key", client=client, sleep=lambda _seconds: None)
    stats = driver.read_mailbox_stats(since=date(2026, 8, 1))
    assert len(stats) == 3


def test_read_mailbox_stats_raises_rate_limit_exceeded_when_retries_run_out() -> None:
    client = queued_client(
        [response(429, "instantly/rate_limited_429.json")] * 10,
        base_url="https://api.instantly.ai",
    )
    driver = InstantlyDriver(api_key="test-key", client=client, sleep=lambda _seconds: None)
    with pytest.raises(RateLimitExceededError):
        driver.read_mailbox_stats(since=date(2026, 8, 1))


def test_throttle_is_unsupported() -> None:
    """Instantly has no per-mailbox daily-limit endpoint."""
    client = queued_client([], base_url="https://api.instantly.ai")
    result = _driver(client).throttle("sender1@example.com", 25)
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.THROTTLE


def test_pause_mailbox_succeeds() -> None:
    client = queued_client(
        [response(200, "instantly/pause_account_200.json")],
        base_url="https://api.instantly.ai",
    )
    result = _driver(client).pause(
        MailboxRef(provider="instantly", mailbox_id="sender1@example.com")
    )
    assert result.outcome == ActionOutcome.PERFORMED
    assert result.capability == Capability.PAUSE


def test_pause_campaign_succeeds() -> None:
    client = queued_client(
        [response(200, "instantly/pause_account_200.json")],
        base_url="https://api.instantly.ai",
    )
    result = _driver(client).pause(CampaignRef(provider="instantly", campaign_id="camp-1"))
    assert result.outcome == ActionOutcome.PERFORMED


def test_pause_failure_is_reported_not_raised() -> None:
    client = queued_client(
        [httpx.Response(500, json={"error": "internal"})],
        base_url="https://api.instantly.ai",
    )
    result = _driver(client).pause(
        MailboxRef(provider="instantly", mailbox_id="sender1@example.com")
    )
    assert result.outcome == ActionOutcome.FAILED


def test_activate_campaign_succeeds() -> None:
    client = queued_client(
        [response(200, "instantly/pause_account_200.json")],
        base_url="https://api.instantly.ai",
    )
    result = _driver(client).activate_campaign("camp-1")
    assert result.outcome == ActionOutcome.PERFORMED


def test_activate_campaign_failure_is_reported_not_raised() -> None:
    client = queued_client(
        [httpx.Response(500, json={"error": "internal"})],
        base_url="https://api.instantly.ai",
    )
    result = _driver(client).activate_campaign("camp-1")
    assert result.outcome == ActionOutcome.FAILED


def test_capabilities_declare_no_throttle() -> None:
    assert Capability.THROTTLE not in InstantlyDriver.capabilities
    assert Capability.PAUSE in InstantlyDriver.capabilities
    assert Capability.READ_STATS in InstantlyDriver.capabilities


def test_fixture_is_valid_json() -> None:
    """Sanity check on the fixture file itself, not the driver."""
    payload = load_json("instantly/analytics_daily_200.json")
    assert isinstance(payload, dict)
    assert isinstance(payload["data"], list)
