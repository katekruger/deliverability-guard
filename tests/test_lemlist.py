"""Tests for providers/lemlist.py, against recorded fixtures only -- no live calls."""

from datetime import date

import httpx
import pytest

from deliverability_guard.providers.base import (
    ActionOutcome,
    CampaignRef,
    Capability,
    MailboxRef,
    MalformedResponseError,
    RateLimitExceededError,
)
from deliverability_guard.providers.lemlist import LemlistDriver
from fixtures.http import recording_client, response

_BASE_URL = "https://api.lemlist.com/api"


def _driver(client: httpx.Client) -> LemlistDriver:
    return LemlistDriver(api_key="super-secret-key", client=client, sleep=lambda _s: None)


def test_read_mailbox_stats_aggregates_sends_and_bounces_per_mailbox_per_day() -> None:
    client, _ = recording_client(
        [response(200, "lemlist/export_activities_200.json")], base_url=_BASE_URL
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")
    by_email = {s.mailbox.mailbox_id: s for s in stats}

    assert by_email["sender1@example.com"].sends == 2
    assert by_email["sender1@example.com"].bounces == 1
    assert by_email["sender1@example.com"].day == date(2026, 8, 1)
    assert by_email["sender2@example.com"].sends == 1
    assert by_email["sender2@example.com"].bounces == 0


def test_read_mailbox_stats_ignores_non_send_non_bounce_activity_types() -> None:
    """An `emailsOpened` event must not be counted as a send or a bounce."""
    client, _ = recording_client(
        [response(200, "lemlist/export_activities_200.json")], base_url=_BASE_URL
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")
    total_sends = sum(s.sends for s in stats)
    total_bounces = sum(s.bounces for s in stats)
    assert total_sends == 3  # 2 for sender1, 1 for sender2
    assert total_bounces == 1


def test_read_mailbox_stats_filters_by_since() -> None:
    client, _ = recording_client(
        [response(200, "lemlist/export_activities_200.json")], base_url=_BASE_URL
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp_123")
    by_email = {s.mailbox.mailbox_id: s for s in stats}
    # sender2's only activity is 2026-07-30, before `since`.
    assert "sender2@example.com" not in by_email


def test_read_mailbox_stats_raises_on_malformed_response() -> None:
    client, _ = recording_client(
        [response(200, "lemlist/export_activities_malformed.json")], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")


def test_read_mailbox_stats_raises_when_status_is_an_error() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError, match="500"):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")


def test_read_mailbox_stats_raises_on_invalid_json_body() -> None:
    client, _ = recording_client([httpx.Response(200, content=b"not json")], base_url=_BASE_URL)
    with pytest.raises(MalformedResponseError, match="JSON"):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")


def test_read_mailbox_stats_accepts_a_bare_list_response() -> None:
    client, _ = recording_client(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "sendUserEmail": "a@example.com",
                        "date": "2026-08-01T00:00:00.000Z",
                        "type": "emailsSent",
                    }
                ],
            )
        ],
        base_url=_BASE_URL,
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")
    assert len(stats) == 1


def test_read_mailbox_stats_retries_past_429_then_succeeds() -> None:
    client, _ = recording_client(
        [
            response(429, "lemlist/rate_limited_429.json"),
            response(200, "lemlist/export_activities_200.json"),
        ],
        base_url=_BASE_URL,
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")
    assert len(stats) == 2


def test_read_mailbox_stats_raises_rate_limit_exceeded_when_retries_run_out() -> None:
    client, _ = recording_client(
        [response(429, "lemlist/rate_limited_429.json")] * 10, base_url=_BASE_URL
    )
    with pytest.raises(RateLimitExceededError):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")


def test_throttle_is_unsupported() -> None:
    client, _ = recording_client([], base_url=_BASE_URL)
    result = _driver(client).throttle("sender1@example.com", 25)
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.THROTTLE


def test_pause_mailbox_is_unsupported() -> None:
    client, _ = recording_client([], base_url=_BASE_URL)
    result = _driver(client).pause(MailboxRef(provider="lemlist", mailbox_id="sender1@example.com"))
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.PAUSE


def test_pause_campaign_succeeds() -> None:
    client, _ = recording_client([httpx.Response(200, json={"ok": True})], base_url=_BASE_URL)
    result = _driver(client).pause(CampaignRef(provider="lemlist", campaign_id="camp_123"))
    assert result.outcome == ActionOutcome.PERFORMED
    assert result.capability == Capability.PAUSE


def test_pause_campaign_failure_is_reported_not_raised() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    result = _driver(client).pause(CampaignRef(provider="lemlist", campaign_id="camp_123"))
    assert result.outcome == ActionOutcome.FAILED


def test_start_campaign_succeeds() -> None:
    client, _ = recording_client([httpx.Response(200, json={"ok": True})], base_url=_BASE_URL)
    result = _driver(client).start_campaign("camp_123")
    assert result.outcome == ActionOutcome.PERFORMED


def test_capabilities_declare_no_throttle() -> None:
    assert Capability.THROTTLE not in LemlistDriver.capabilities
    assert Capability.PAUSE in LemlistDriver.capabilities
    assert Capability.WEBHOOKS not in LemlistDriver.capabilities


# --- Security: the api_key must never appear in a raised/returned message ---


def test_pause_failure_message_never_contains_the_api_key() -> None:
    client, requests = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    result = _driver(client).pause(CampaignRef(provider="lemlist", campaign_id="camp_123"))
    assert "super-secret-key" not in result.detail
    # The key really was sent via Basic auth (so we're testing the real
    # risk, not a no-op) -- just never let it leak back into anything
    # raised or returned.
    assert requests[0].headers["Authorization"].startswith("Basic ")


def test_malformed_response_error_never_contains_the_api_key() -> None:
    client, _ = recording_client(
        [response(200, "lemlist/export_activities_malformed.json")], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError) as exc_info:
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp_123")
    assert "super-secret-key" not in str(exc_info.value)
