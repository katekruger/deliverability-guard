"""Tests for providers/smartlead.py, against recorded fixtures only -- no live calls."""

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
from deliverability_guard.providers.smartlead import SmartleadDriver
from fixtures.http import recording_client, response


def _driver(client: httpx.Client) -> SmartleadDriver:
    return SmartleadDriver(api_key="super-secret-key", client=client, sleep=lambda _s: None)


def test_read_mailbox_stats_parses_a_normal_response_and_filters_by_since() -> None:
    client, _ = recording_client(
        [response(200, "smartlead/campaign_statistics_200.json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")
    by_email = {s.mailbox.mailbox_id: s for s in stats}

    assert by_email["sender1@example.com"].sends == 500
    assert by_email["sender1@example.com"].bounces == 4
    # sender3's row is dated 2026-07-25, before `since` -- filtered out.
    assert "sender3@example.com" not in by_email


def test_read_mailbox_stats_flags_a_disconnected_mailbox() -> None:
    client, _ = recording_client(
        [response(200, "smartlead/campaign_statistics_200.json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")
    by_email = {s.mailbox.mailbox_id: s for s in stats}
    assert by_email["sender2@example.com"].status == MailboxStatus.DISCONNECTED


def test_read_mailbox_stats_raises_on_malformed_response() -> None:
    client, _ = recording_client(
        [response(200, "smartlead/campaign_statistics_malformed.json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    with pytest.raises(MalformedResponseError):
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")


def test_read_mailbox_stats_raises_when_status_is_an_error() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})],
        base_url="https://server.smartlead.ai/api/v1",
    )
    with pytest.raises(MalformedResponseError, match="500"):
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")


def test_read_mailbox_stats_raises_on_invalid_json_body() -> None:
    client, _ = recording_client(
        [httpx.Response(200, content=b"not json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    with pytest.raises(MalformedResponseError, match="JSON"):
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")


def test_read_mailbox_stats_accepts_a_bare_list_response() -> None:
    client, _ = recording_client(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "from_email": "a@example.com",
                        "sent_date": "2026-08-01",
                        "sent_count": 5,
                        "bounce_count": 0,
                    }
                ],
            )
        ],
        base_url="https://server.smartlead.ai/api/v1",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")
    assert len(stats) == 1


def test_read_mailbox_stats_retries_past_429_then_succeeds() -> None:
    client, _ = recording_client(
        [
            response(429, "smartlead/rate_limited_429.json"),
            response(200, "smartlead/campaign_statistics_200.json"),
        ],
        base_url="https://server.smartlead.ai/api/v1",
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")
    assert len(stats) == 2


def test_read_mailbox_stats_raises_rate_limit_exceeded_when_retries_run_out() -> None:
    client, _ = recording_client(
        [response(429, "smartlead/rate_limited_429.json")] * 10,
        base_url="https://server.smartlead.ai/api/v1",
    )
    with pytest.raises(RateLimitExceededError):
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")


def test_throttle_succeeds() -> None:
    """The daily-limit endpoint: the difference between a circuit breaker
    and a kill switch."""
    client, _ = recording_client(
        [response(200, "smartlead/email_account_update_200.json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    result = _driver(client).throttle("acct-1", 25)
    assert result.outcome == ActionOutcome.PERFORMED
    assert result.capability == Capability.THROTTLE


def test_throttle_rejects_negative_daily_limit() -> None:
    client, _ = recording_client([], base_url="https://server.smartlead.ai/api/v1")
    with pytest.raises(ValueError, match="daily_limit"):
        _driver(client).throttle("acct-1", -1)


def test_pause_mailbox_is_unsupported() -> None:
    """Provider can only throttle (Smartlead) -> the pause rung is skipped
    for a per-mailbox target, even though a campaign-wide pause exists."""
    client, _ = recording_client([], base_url="https://server.smartlead.ai/api/v1")
    result = _driver(client).pause(MailboxRef(provider="smartlead", mailbox_id="acct-1"))
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.PAUSE


def test_pause_campaign_succeeds() -> None:
    client, _ = recording_client(
        [response(200, "smartlead/campaign_status_200.json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    result = _driver(client).pause(CampaignRef(provider="smartlead", campaign_id="camp-1"))
    assert result.outcome == ActionOutcome.PERFORMED


def test_pause_campaign_failure_is_reported_not_raised() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})],
        base_url="https://server.smartlead.ai/api/v1",
    )
    result = _driver(client).pause(CampaignRef(provider="smartlead", campaign_id="camp-1"))
    assert result.outcome == ActionOutcome.FAILED


def test_activate_campaign_succeeds() -> None:
    client, _ = recording_client(
        [response(200, "smartlead/campaign_status_200.json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    result = _driver(client).activate_campaign("camp-1")
    assert result.outcome == ActionOutcome.PERFORMED


def test_capabilities_declare_no_pause() -> None:
    assert Capability.PAUSE not in SmartleadDriver.capabilities
    assert Capability.THROTTLE in SmartleadDriver.capabilities


# --- Security: the api_key must never appear in a raised/returned message ---


def test_throttle_failure_message_never_contains_the_api_key() -> None:
    client, requests = recording_client(
        [httpx.Response(500, json={"error": "internal"})],
        base_url="https://server.smartlead.ai/api/v1",
    )
    result = _driver(client).throttle("acct-1", 25)
    assert result.outcome == ActionOutcome.FAILED
    assert "super-secret-key" not in result.detail
    # The key really was sent (so we're testing the real risk, not a no-op) --
    # just never let it leak back out into anything we log or raise.
    assert requests[0].url.params.get("api_key") == "super-secret-key"


def test_malformed_response_error_never_contains_the_api_key() -> None:
    client, requests = recording_client(
        [response(200, "smartlead/campaign_statistics_malformed.json")],
        base_url="https://server.smartlead.ai/api/v1",
    )
    with pytest.raises(MalformedResponseError) as exc_info:
        _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")
    assert "super-secret-key" not in str(exc_info.value)
    assert requests[0].url.params.get("api_key") == "super-secret-key"
