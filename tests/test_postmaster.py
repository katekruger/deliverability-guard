"""Tests for signals/postmaster.py, against fixtures whose SHAPE is verified
against the real Postmaster v2 discovery document (see
tests/fixtures/postmaster/README.md) -- no live calls."""

from datetime import date

import httpx
import pytest

from deliverability_guard.providers.base import MalformedResponseError, RateLimitExceededError
from deliverability_guard.signals.postmaster import (
    ComplianceState,
    DomainNotVerifiedError,
    PostmasterClient,
    TokenExpiredError,
    TokenProvider,
    forces_hard_gate,
)
from fixtures.http import queued_client, response


def _token_provider(token: str = "test-token") -> TokenProvider:
    calls = {"refresh": 0}

    def get_token() -> str:
        return token

    def refresh() -> None:
        calls["refresh"] += 1

    provider = TokenProvider(get_token=get_token, refresh=refresh)
    provider.refresh_calls = calls  # type: ignore[attr-defined]
    return provider


def _client(
    httpx_client: httpx.Client, *, token_provider: TokenProvider | None = None
) -> PostmasterClient:
    return PostmasterClient(
        token_provider=token_provider or _token_provider(),
        client=httpx_client,
        sleep=lambda _s: None,
    )


_BASE_URL = "https://gmailpostmastertools.googleapis.com"


# --- query_domain_stats ----------------------------------------------------


def test_query_domain_stats_parses_a_normal_response() -> None:
    client = queued_client(
        [response(200, "postmaster/domain_stats_query_200.json")], base_url=_BASE_URL
    )
    rows = _client(client).query_domain_stats(
        "example.com",
        metric_names=["spam_rate", "auth_success_rate", "delivery_error_count"],
        since=date(2026, 8, 1),
        until=date(2026, 8, 3),
    )
    assert len(rows) == 4
    days = {r.day for r in rows}
    assert date(2026, 8, 2) not in days  # the gap: no row, not a zero row


def test_query_domain_stats_parses_int_value_as_a_string_encoded_int64() -> None:
    client = queued_client(
        [response(200, "postmaster/domain_stats_query_200.json")], base_url=_BASE_URL
    )
    rows = _client(client).query_domain_stats(
        "example.com",
        metric_names=["delivery_error_count"],
        since=date(2026, 8, 1),
        until=date(2026, 8, 3),
    )
    error_count_rows = [r for r in rows if r.metric_name == "delivery_error_count"]
    assert error_count_rows[0].value == 12.0


def test_query_domain_stats_follows_pagination() -> None:
    client = queued_client(
        [
            response(200, "postmaster/domain_stats_query_paginated_page1.json"),
            response(200, "postmaster/domain_stats_query_paginated_page2.json"),
        ],
        base_url=_BASE_URL,
    )
    rows = _client(client).query_domain_stats(
        "example.com",
        metric_names=["spam_rate"],
        since=date(2026, 8, 1),
        until=date(2026, 8, 2),
    )
    assert len(rows) == 2
    assert {r.day for r in rows} == {date(2026, 8, 1), date(2026, 8, 2)}


def test_query_domain_stats_raises_on_malformed_response() -> None:
    client = queued_client(
        [response(200, "postmaster/domain_stats_query_malformed.json")], base_url=_BASE_URL
    )
    with pytest.raises(MalformedResponseError):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


def test_query_domain_stats_raises_domain_not_verified_on_403() -> None:
    client = queued_client([httpx.Response(403, json={"error": "denied"})], base_url=_BASE_URL)
    with pytest.raises(DomainNotVerifiedError):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


def test_query_domain_stats_retries_past_429() -> None:
    client = queued_client(
        [
            response(429, "postmaster/rate_limited_429.json"),
            response(200, "postmaster/domain_stats_query_200.json"),
        ],
        base_url=_BASE_URL,
    )
    rows = _client(client).query_domain_stats(
        "example.com", metric_names=["spam_rate"], since=date(2026, 8, 1), until=date(2026, 8, 3)
    )
    assert len(rows) == 4


def test_query_domain_stats_raises_rate_limit_exceeded_when_retries_run_out() -> None:
    client = queued_client(
        [response(429, "postmaster/rate_limited_429.json")] * 10, base_url=_BASE_URL
    )
    with pytest.raises(RateLimitExceededError):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


def test_query_domain_stats_rejects_page_size_over_200() -> None:
    client = queued_client([], base_url=_BASE_URL)
    with pytest.raises(ValueError, match="page_size"):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
            page_size=201,
        )


def test_query_domain_stats_rejects_invalid_granularity() -> None:
    client = queued_client([], base_url=_BASE_URL)
    with pytest.raises(ValueError, match="granularity"):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
            granularity="WEEKLY",
        )


# --- get_compliance_status and the hard gate --------------------------------


def test_get_compliance_status_parses_needs_work_with_reason() -> None:
    client = queued_client(
        [response(200, "postmaster/compliance_status_needs_work.json")], base_url=_BASE_URL
    )
    status = _client(client).get_compliance_status("example.com")
    assert status.deliverability.status == ComplianceState.NEEDS_WORK
    assert status.deliverability.reason == "SPAM_RATE_HIGH"
    assert status.honor_unsubscribe.status == ComplianceState.NEEDS_WORK
    assert status.one_click_unsubscribe.status == ComplianceState.COMPLIANT


def test_get_compliance_status_parses_all_compliant() -> None:
    client = queued_client(
        [response(200, "postmaster/compliance_status_compliant.json")], base_url=_BASE_URL
    )
    status = _client(client).get_compliance_status("example.com")
    assert status.deliverability.status == ComplianceState.COMPLIANT
    assert status.deliverability.reason is None


def test_get_compliance_status_treats_a_missing_verdict_as_unspecified_not_compliant() -> None:
    client = queued_client(
        [response(200, "postmaster/compliance_status_missing_verdict.json")], base_url=_BASE_URL
    )
    status = _client(client).get_compliance_status("example.com")
    assert status.one_click_unsubscribe.status == ComplianceState.STATE_UNSPECIFIED


def test_get_compliance_status_raises_domain_not_verified_on_403() -> None:
    client = queued_client([httpx.Response(403, json={"error": "denied"})], base_url=_BASE_URL)
    with pytest.raises(DomainNotVerifiedError):
        _client(client).get_compliance_status("example.com")


def test_forces_hard_gate_true_when_deliverability_needs_work() -> None:
    client = queued_client(
        [response(200, "postmaster/compliance_status_needs_work.json")], base_url=_BASE_URL
    )
    status = _client(client).get_compliance_status("example.com")
    assert forces_hard_gate(status) is True


def test_forces_hard_gate_false_when_compliant() -> None:
    client = queued_client(
        [response(200, "postmaster/compliance_status_compliant.json")], base_url=_BASE_URL
    )
    status = _client(client).get_compliance_status("example.com")
    assert forces_hard_gate(status) is False


def test_forces_hard_gate_ignores_unsubscribe_verdicts() -> None:
    """Only deliverabilityStatusVerdict gates -- honor_unsubscribe needing
    work (present in this fixture) is a slow-loop signal, not a hard gate."""
    client = queued_client(
        [response(200, "postmaster/compliance_status_needs_work.json")], base_url=_BASE_URL
    )
    status = _client(client).get_compliance_status("example.com")
    assert status.honor_unsubscribe.status == ComplianceState.NEEDS_WORK
    assert status.deliverability.status == ComplianceState.NEEDS_WORK  # what actually gates
    assert forces_hard_gate(status) is True  # true because of deliverability, not honor_unsubscribe


# --- Domain onboarding: create / verify / getVerificationToken -------------


def test_create_domain_succeeds() -> None:
    client = queued_client(
        [httpx.Response(200, json={"name": "domains/example.com"})], base_url=_BASE_URL
    )
    _client(client).create_domain("example.com")  # must not raise


def test_get_verification_token_parses_the_token() -> None:
    client = queued_client(
        [response(200, "postmaster/verification_token_200.json")], base_url=_BASE_URL
    )
    token = _client(client).get_verification_token("example.com")
    assert token == "google-site-verification=abc123def456"


def test_get_verification_token_rejects_an_invalid_method() -> None:
    client = queued_client([], base_url=_BASE_URL)
    with pytest.raises(ValueError, match="method"):
        _client(client).get_verification_token("example.com", method="SRV")


def test_verify_domain_true_on_200() -> None:
    client = queued_client([httpx.Response(200, json={})], base_url=_BASE_URL)
    assert _client(client).verify_domain("example.com") is True


def test_verify_domain_false_on_failure_status() -> None:
    client = queued_client([httpx.Response(400, json={"error": "not ready"})], base_url=_BASE_URL)
    assert _client(client).verify_domain("example.com") is False


def test_verify_domain_rejects_an_invalid_method() -> None:
    client = queued_client([], base_url=_BASE_URL)
    with pytest.raises(ValueError, match="method"):
        _client(client).verify_domain("example.com", method="SRV")


# --- OAuth token expiry mid-run ---------------------------------------------


def test_a_401_triggers_exactly_one_refresh_and_retry() -> None:
    client = queued_client(
        [
            httpx.Response(401, json={"error": "expired"}),
            response(200, "postmaster/domain_stats_query_200.json"),
        ],
        base_url=_BASE_URL,
    )
    provider = _token_provider()
    rows = _client(client, token_provider=provider).query_domain_stats(
        "example.com", metric_names=["spam_rate"], since=date(2026, 8, 1), until=date(2026, 8, 3)
    )
    assert len(rows) == 4
    assert provider.refresh_calls["refresh"] == 1  # type: ignore[attr-defined]


def test_still_401_after_refresh_raises_token_expired_not_a_crash() -> None:
    client = queued_client(
        [
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(401, json={"error": "still expired"}),
        ],
        base_url=_BASE_URL,
    )
    with pytest.raises(TokenExpiredError):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


# --- Generic (non-403) error statuses ---------------------------------------


def test_query_domain_stats_raises_malformed_on_a_generic_error_status() -> None:
    client = queued_client([httpx.Response(500, json={"error": "oops"})], base_url=_BASE_URL)
    with pytest.raises(MalformedResponseError):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


def test_get_compliance_status_raises_malformed_on_a_generic_error_status() -> None:
    client = queued_client([httpx.Response(500, json={"error": "oops"})], base_url=_BASE_URL)
    with pytest.raises(MalformedResponseError):
        _client(client).get_compliance_status("example.com")


def test_create_domain_raises_malformed_on_a_generic_error_status() -> None:
    client = queued_client([httpx.Response(500, json={"error": "oops"})], base_url=_BASE_URL)
    with pytest.raises(MalformedResponseError):
        _client(client).create_domain("example.com")


def test_get_verification_token_raises_malformed_on_a_generic_error_status() -> None:
    client = queued_client([httpx.Response(500, json={"error": "oops"})], base_url=_BASE_URL)
    with pytest.raises(MalformedResponseError):
        _client(client).get_verification_token("example.com")


def test_raises_on_invalid_json_body() -> None:
    client = queued_client([httpx.Response(200, content=b"not json")], base_url=_BASE_URL)
    with pytest.raises(MalformedResponseError, match="JSON"):
        _client(client).get_compliance_status("example.com")


# --- StatisticValue parsing --------------------------------------------


def test_parses_a_float_value() -> None:
    client = queued_client(
        [
            httpx.Response(
                200,
                json={
                    "domainStats": [
                        {
                            "metric": "spam_rate",
                            "value": {"floatValue": 0.001},
                            "date": {"year": 2026, "month": 8, "day": 1},
                        }
                    ]
                },
            )
        ],
        base_url=_BASE_URL,
    )
    rows = _client(client).query_domain_stats(
        "example.com",
        metric_names=["spam_rate"],
        since=date(2026, 8, 1),
        until=date(2026, 8, 1),
    )
    assert rows[0].value == pytest.approx(0.001)


def test_rejects_a_statistic_value_with_no_numeric_field() -> None:
    client = queued_client(
        [
            httpx.Response(
                200, json={"domainStats": [{"metric": "spam_rate", "value": {"stringValue": "x"}}]}
            )
        ],
        base_url=_BASE_URL,
    )
    with pytest.raises(MalformedResponseError, match="none of"):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


def test_rejects_an_int_value_that_is_not_a_string() -> None:
    client = queued_client(
        [
            httpx.Response(
                200, json={"domainStats": [{"metric": "spam_rate", "value": {"intValue": 12}}]}
            )
        ],
        base_url=_BASE_URL,
    )
    with pytest.raises(MalformedResponseError, match="intValue"):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


def test_rejects_an_int_value_that_does_not_parse_as_an_integer() -> None:
    client = queued_client(
        [
            httpx.Response(
                200,
                json={
                    "domainStats": [{"metric": "spam_rate", "value": {"intValue": "not-a-number"}}]
                },
            )
        ],
        base_url=_BASE_URL,
    )
    with pytest.raises(MalformedResponseError, match="not an integer"):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


def test_rejects_a_non_numeric_double_value() -> None:
    client = queued_client(
        [
            httpx.Response(
                200,
                json={
                    "domainStats": [
                        {"metric": "spam_rate", "value": {"doubleValue": "not-a-number"}}
                    ]
                },
            )
        ],
        base_url=_BASE_URL,
    )
    with pytest.raises(MalformedResponseError, match="numeric"):
        _client(client).query_domain_stats(
            "example.com",
            metric_names=["spam_rate"],
            since=date(2026, 8, 1),
            until=date(2026, 8, 1),
        )


# --- Date parsing --------------------------------------------------------


def test_a_row_with_no_date_has_none_day() -> None:
    """OVERALL granularity: no per-date breakdown."""
    client = queued_client(
        [
            httpx.Response(
                200,
                json={"domainStats": [{"metric": "spam_rate", "value": {"doubleValue": 0.001}}]},
            )
        ],
        base_url=_BASE_URL,
    )
    rows = _client(client).query_domain_stats(
        "example.com",
        metric_names=["spam_rate"],
        since=date(2026, 8, 1),
        until=date(2026, 8, 1),
        granularity="OVERALL",
    )
    assert rows[0].day is None
    assert rows[0].source_day is None


def test_a_partial_date_with_zero_year_has_none_day_but_keeps_source() -> None:
    client = queued_client(
        [
            httpx.Response(
                200,
                json={
                    "domainStats": [
                        {
                            "metric": "spam_rate",
                            "value": {"doubleValue": 0.001},
                            "date": {"year": 0, "month": 8, "day": 1},
                        }
                    ]
                },
            )
        ],
        base_url=_BASE_URL,
    )
    rows = _client(client).query_domain_stats(
        "example.com",
        metric_names=["spam_rate"],
        since=date(2026, 8, 1),
        until=date(2026, 8, 1),
    )
    assert rows[0].day is None
    assert rows[0].source_day == "0000-08-01"


# --- Verdict parsing edge cases -------------------------------------------


def test_rejects_a_verdict_with_a_non_string_status() -> None:
    client = queued_client(
        [
            httpx.Response(
                200,
                json={
                    "complianceData": {"deliverabilityStatusVerdict": {"state": {"status": 123}}}
                },
            )
        ],
        base_url=_BASE_URL,
    )
    with pytest.raises(MalformedResponseError, match="status"):
        _client(client).get_compliance_status("example.com")


def test_rejects_an_unknown_compliance_status_value() -> None:
    client = queued_client(
        [
            httpx.Response(
                200,
                json={
                    "complianceData": {
                        "deliverabilityStatusVerdict": {"state": {"status": "SOMETHING_NEW"}}
                    }
                },
            )
        ],
        base_url=_BASE_URL,
    )
    with pytest.raises(MalformedResponseError, match="unknown"):
        _client(client).get_compliance_status("example.com")
