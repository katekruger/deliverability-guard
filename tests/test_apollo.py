"""Tests for providers/apollo.py, against recorded fixtures only -- no live calls."""

from datetime import date

import httpx
import pytest

from deliverability_guard.providers.apollo import ApolloDriver
from deliverability_guard.providers.base import (
    ActionOutcome,
    CampaignRef,
    Capability,
    MailboxRef,
    MalformedResponseError,
    RateLimitExceededError,
)
from fixtures.http import recording_client, response

_BASE_URL = "https://api.apollo.io/v1"


def _driver(client: httpx.Client) -> ApolloDriver:
    return ApolloDriver(api_key="super-secret-key", client=client, sleep=lambda _s: None)


def test_read_mailbox_stats_parses_a_normal_response_and_filters_by_since() -> None:
    client, _ = recording_client([response(200, "apollo/daily_stats_200.json")], base_url=_BASE_URL)
    stats = _driver(client).read_mailbox_stats(since=date(2026, 8, 1), campaign_id="camp-1")
    by_email = {s.mailbox.mailbox_id: s for s in stats}

    assert by_email["sender1@example.com"].sends == 40
    assert by_email["sender1@example.com"].bounces == 1
    # sender2's row is dated 2026-07-25, before `since` -- filtered out.
    assert "sender2@example.com" not in by_email


def test_read_mailbox_stats_raises_on_malformed_response() -> None:
    client, _ = recording_client(
        [response(200, "apollo/daily_stats_malformed.json")], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp-1")


def test_read_mailbox_stats_raises_when_status_is_an_error() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError, match="500"):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp-1")


def test_read_mailbox_stats_raises_on_invalid_json_body() -> None:
    client, _ = recording_client([httpx.Response(200, content=b"not json")], base_url=_BASE_URL)
    with pytest.raises(MalformedResponseError, match="JSON"):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp-1")


def test_read_mailbox_stats_retries_past_429_then_succeeds() -> None:
    client, _ = recording_client(
        [
            response(429, "apollo/rate_limited_429.json"),
            response(200, "apollo/daily_stats_200.json"),
        ],
        base_url=_BASE_URL,
    )
    stats = _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp-1")
    assert len(stats) == 2


def test_read_mailbox_stats_raises_rate_limit_exceeded_when_retries_run_out() -> None:
    client, _ = recording_client(
        [response(429, "apollo/rate_limited_429.json")] * 10, base_url=_BASE_URL
    )
    with pytest.raises(RateLimitExceededError):
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp-1")


def test_throttle_is_unsupported() -> None:
    client, _ = recording_client([], base_url=_BASE_URL)
    result = _driver(client).throttle("sender1@example.com", 25)
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.THROTTLE


def test_pause_mailbox_is_unsupported() -> None:
    """Apollo can only list mailboxes, not act on one individually."""
    client, _ = recording_client([], base_url=_BASE_URL)
    result = _driver(client).pause(MailboxRef(provider="apollo", mailbox_id="sender1@example.com"))
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.PAUSE


def test_pause_campaign_calls_abort() -> None:
    client, _ = recording_client([httpx.Response(200, json={"ok": True})], base_url=_BASE_URL)
    result = _driver(client).pause(CampaignRef(provider="apollo", campaign_id="camp-1"))
    assert result.outcome == ActionOutcome.PERFORMED
    assert result.capability == Capability.PAUSE


def test_abort_sequence_failure_is_reported_not_raised() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    result = _driver(client).abort_sequence("camp-1")
    assert result.outcome == ActionOutcome.FAILED


def test_activate_campaign_succeeds_and_notes_unverified_semantics() -> None:
    client, _ = recording_client([httpx.Response(200, json={"ok": True})], base_url=_BASE_URL)
    result = _driver(client).activate_campaign("camp-1")
    assert result.outcome == ActionOutcome.PERFORMED
    assert "unverified" in result.detail


def test_activate_campaign_failure_is_reported_not_raised() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    result = _driver(client).activate_campaign("camp-1")
    assert result.outcome == ActionOutcome.FAILED


def test_list_email_accounts_parses_a_normal_response() -> None:
    client, _ = recording_client(
        [response(200, "apollo/email_accounts_200.json")], base_url=_BASE_URL
    )
    accounts = _driver(client).list_email_accounts()
    assert accounts == [
        MailboxRef(provider="apollo", mailbox_id="sender1@example.com"),
        MailboxRef(provider="apollo", mailbox_id="sender2@example.com"),
    ]


def test_list_email_accounts_raises_when_status_is_an_error() -> None:
    client, _ = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError, match="500"):
        _driver(client).list_email_accounts()


def test_capabilities_declare_no_throttle_or_webhooks() -> None:
    assert Capability.THROTTLE not in ApolloDriver.capabilities
    assert Capability.WEBHOOKS not in ApolloDriver.capabilities
    assert Capability.PAUSE in ApolloDriver.capabilities
    assert Capability.READ_STATS in ApolloDriver.capabilities


# --- Security: the api_key must never appear in a raised/returned message ---


def test_abort_failure_message_never_contains_the_api_key() -> None:
    client, requests = recording_client(
        [httpx.Response(500, json={"error": "internal"})], base_url=_BASE_URL
    )
    result = _driver(client).abort_sequence("camp-1")
    assert "super-secret-key" not in result.detail
    assert requests[0].headers["X-Api-Key"] == "super-secret-key"


def test_malformed_response_error_never_contains_the_api_key() -> None:
    client, _ = recording_client(
        [response(200, "apollo/daily_stats_malformed.json")], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError) as exc_info:
        _driver(client).read_mailbox_stats(since=date(2026, 7, 1), campaign_id="camp-1")
    assert "super-secret-key" not in str(exc_info.value)
