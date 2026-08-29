"""Google Postmaster Tools v2 client.

    POST /v2/{parent=domains/*}/domainStats:query   (pageSize max 200; DAILY | OVERALL)
    POST /v2/domainStats:batchQuery
    GET  /v2/{name=domains/*/complianceStatus}
         /v2/domains -- create, verify, getVerificationToken

Metrics: SPAM_RATE, FEEDBACK_LOOP_ID, FEEDBACK_LOOP_SPAM_RATE,
AUTH_SUCCESS_RATE, TLS_ENCRYPTION_RATE, DELIVERY_ERROR_RATE,
DELIVERY_ERROR_COUNT. There is NO `DOMAIN_REPUTATION` and NO
`IP_REPUTATION` metric in v2 -- Google removed both. This module does not
look for either. See docs/postmaster-verdicts.md, verified against the live
discovery document (2026-08-29), for the full enumeration this module is
built against, including the confirmed regression that v2's `SPAM_RATE` no
longer carries the confidence bounds v1 had -- every rate this module
returns is a bare point estimate, fed into `engine/posterior.py` like any
other rate, never treated as pre-vetted just because it came from Google.

`domainStats:query` "[r]eturns statistics only for dates where data is
available" (Google's own method description). Gaps are normal, not errors:
`query_domain_stats` returns exactly the rows the API returns and never
synthesizes a zero-value row for a missing date. Treating an absent day as
"0%, therefore healthy" is exactly the coercion AGENTS.md prohibits.

`get_compliance_status` wires `getComplianceStatus` as the hard gate
(BUILD-PLAN.md §5): `forces_hard_gate` below is what
`engine.breaker.evaluate`'s `compliance_gate_tripped` parameter is meant to
receive. If Google is telling you directly that a domain's deliverability
verdict needs work, that outranks any statistical inference from this
project's own posterior, regardless of volume.

The programmatic `create_domain` / `get_verification_token` / `verify_domain`
flow matters because it means multi-tenant onboarding without walking each
user through the Postmaster web UI by hand.
"""

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum, auto

import httpx

from deliverability_guard.engine.state import DataState
from deliverability_guard.providers._parsing import (
    require_dict,
    require_int,
    require_list,
    require_str,
)
from deliverability_guard.providers._retry import request_with_retry
from deliverability_guard.providers.base import MalformedResponseError, ProviderError

_BASE_URL = "https://gmailpostmastertools.googleapis.com"
_PROVIDER = "postmaster"
_MAX_PAGE_SIZE = 200


class DomainNotVerifiedError(ProviderError):
    """The domain isn't verified/accessible in this Postmaster account.

    Google's own method description for `domainStats.query` states it
    "[r]eturns PERMISSION_DENIED if you don't have permission to access
    DomainStats for the domain" -- mapped here from HTTP 403. Call
    `create_domain` / `get_verification_token` / `verify_domain` first.
    """


class TokenExpiredError(ProviderError):
    """The OAuth token was rejected even after one refresh attempt."""


@dataclass
class TokenProvider:
    """Wraps however the caller fetches/refreshes an OAuth token.

    `get_token` should return whatever token the provider currently
    believes is valid (cached or not, that's the caller's business).
    `refresh` is called explicitly on a 401 so the provider knows its
    cached token is stale, not just asked for the same one again -- "OAuth
    token expiry mid-run -> refresh, don't crash the loop" (BUILD-PLAN.md
    §8) needs a way to distinguish those two asks.
    """

    get_token: Callable[[], str]
    refresh: Callable[[], None] = lambda: None


class ComplianceState(Enum):
    STATE_UNSPECIFIED = auto()
    COMPLIANT = auto()
    NEEDS_WORK = auto()


@dataclass(frozen=True, slots=True)
class ComplianceVerdict:
    status: ComplianceState
    reason: str | None


@dataclass(frozen=True, slots=True)
class DomainComplianceStatus:
    domain: str
    deliverability: ComplianceVerdict
    one_click_unsubscribe: ComplianceVerdict
    honor_unsubscribe: ComplianceVerdict


def forces_hard_gate(status: DomainComplianceStatus) -> bool:
    """Google telling you directly you're non-compliant outranks any
    statistical inference (BUILD-PLAN.md §5) -- trip regardless of volume.

    Deliberately keyed on `deliverability` alone, not the other two
    verdicts: `deliverabilityStatusVerdict` is Google's own aggregate
    assessment (its `SPAM_RATE_HIGH` reason is keyed to the same 0.1% this
    project's `throttle` rung already uses, see docs/postmaster-verdicts.md
    §1), while unsubscribe-compliance verdicts are real risk signals but not
    themselves evidence of an active reputation emergency -- those feed the
    slow loop's threshold tuning instead (see loops/slow.py).
    """
    return status.deliverability.status is ComplianceState.NEEDS_WORK


@dataclass(frozen=True, slots=True)
class DomainStatRow:
    """One row from `domainStats:query`.

    `day` is `None` when the request used `OVERALL` granularity (no daily
    breakdown), OR when Postmaster returned a partial `Date` (year/month/day
    with a zero component -- its own schema allows this for things like "a
    year on its own"). `source_day` preserves whatever Google actually sent,
    verbatim, in both cases.
    """

    metric_name: str
    day: date | None
    value: float
    source_day: str | None


@dataclass(frozen=True, slots=True)
class DayAvailability:
    day: date
    state: DataState
    transition_alert: bool
    """True exactly on the day this metric transitions from having data to
    not having it. See `coverage_over_range` below."""


def coverage_over_range(
    rows: Sequence[DomainStatRow],
    *,
    metric_name: str,
    since: date,
    until: date,
) -> list[DayAvailability]:
    """Per-day OK/INSUFFICIENT_DATA/STALE for one metric over a date range,
    treating a present-to-absent transition as its own alert -- the same
    shape as `engine.state.evaluate_stream` (Prompt 1), reimplemented here
    because Postmaster rows carry a bare rate value rather than
    sends/complaints counts, so `engine.state.DailyReport` doesn't apply
    directly.

    This exists for the landmine BUILD-PLAN.md §8 and §9 both call out: a
    domain that gets throttled sends less, can drop below Postmaster's
    (unpublished) privacy threshold as a direct consequence, and disappear
    from `domainStats:query` results entirely -- monitoring goes dark
    exactly when things are worst. A missing day is never coerced to "0%,
    therefore healthy"; a transition into missing days is its own alert.
    """
    if since > until:
        raise ValueError(f"since ({since}) must not be after until ({until})")
    present_days = {
        row.day for row in rows if row.metric_name == metric_name and row.day is not None
    }
    results: list[DayAvailability] = []
    previously_had_data = False
    current = since
    one_day = timedelta(days=1)
    while current <= until:
        has_data = current in present_days
        if has_data:
            state = DataState.OK
            transition_alert = False
        elif previously_had_data:
            state = DataState.STALE
            transition_alert = True
        else:
            state = DataState.INSUFFICIENT_DATA
            transition_alert = False
        results.append(DayAvailability(day=current, state=state, transition_alert=transition_alert))
        previously_had_data = has_data
        current += one_day
    return results


class PostmasterClient:
    name = _PROVIDER

    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: random.Random | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._client = client or httpx.Client(base_url=_BASE_URL, timeout=30.0)
        self._sleep = sleep
        self._rand = rand

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider.get_token()}"}

    def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None, json: object = None
    ) -> httpx.Response:
        def send() -> httpx.Response:
            return self._client.request(
                method, path, headers=self._headers(), params=params, json=json
            )

        response = request_with_retry(send, sleep=self._sleep, rand=self._rand)
        if response.status_code == 401:
            # Token expiry mid-run: refresh once and retry, don't crash the loop.
            self._token_provider.refresh()
            response = request_with_retry(send, sleep=self._sleep, rand=self._rand)
            if response.status_code == 401:
                raise TokenExpiredError(f"{_PROVIDER}: still unauthorized after one token refresh")
        return response

    def query_domain_stats(
        self,
        domain: str,
        *,
        metric_names: Sequence[str],
        since: date,
        until: date,
        granularity: str = "DAILY",
        page_size: int = _MAX_PAGE_SIZE,
    ) -> list[DomainStatRow]:
        """Gaps are normal, not errors: returns exactly the rows Google
        returns. A date with no traffic produces no row at all -- never a
        row with value 0."""
        if page_size > _MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be <= {_MAX_PAGE_SIZE}, got {page_size}")
        if granularity not in ("DAILY", "OVERALL"):
            raise ValueError(f"granularity must be 'DAILY' or 'OVERALL', got {granularity!r}")

        request_body: dict[str, object] = {
            "metricDefinitions": [
                {"name": name, "baseMetric": {"standardMetric": name}} for name in metric_names
            ],
            "timeQuery": {
                "dateRanges": {
                    "dateRanges": [{"start": _to_api_date(since), "end": _to_api_date(until)}]
                }
            },
            "aggregationGranularity": granularity,
            "pageSize": page_size,
        }

        rows: list[DomainStatRow] = []
        page_token: str | None = None
        while True:
            body = dict(request_body)
            if page_token is not None:
                body["pageToken"] = page_token
            response = self._request("POST", f"/v2/domains/{domain}/domainStats:query", json=body)
            if response.status_code == 403:
                raise DomainNotVerifiedError(
                    f"{_PROVIDER}: permission denied for domain {domain!r} -- not "
                    f"verified/accessible in this account"
                )
            if response.status_code != 200:
                raise MalformedResponseError(
                    f"{_PROVIDER}: domainStats:query returned status {response.status_code}"
                )
            payload = _parse_json(response)
            parsed = require_dict(payload, _PROVIDER, "domainStats:query response")
            raw_rows = parsed.get("domainStats", [])
            for raw_row in require_list(raw_rows, _PROVIDER, "'domainStats'"):
                rows.append(_parse_domain_stat(raw_row))
            next_token = parsed.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token
        return rows

    def get_compliance_status(self, domain: str) -> DomainComplianceStatus:
        response = self._request("GET", f"/v2/domains/{domain}/complianceStatus")
        if response.status_code == 403:
            raise DomainNotVerifiedError(
                f"{_PROVIDER}: permission denied for domain {domain!r} -- not "
                f"verified/accessible in this account"
            )
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: getComplianceStatus returned status {response.status_code}"
            )
        payload = _parse_json(response)
        body = require_dict(payload, _PROVIDER, "complianceStatus response")
        compliance_data = require_dict(
            body.get("complianceData", {}), _PROVIDER, "'complianceData'"
        )
        return DomainComplianceStatus(
            domain=domain,
            deliverability=_parse_verdict(
                compliance_data.get("deliverabilityStatusVerdict"), status_key="state"
            ),
            one_click_unsubscribe=_parse_verdict(
                compliance_data.get("oneClickUnsubscribeVerdict"), status_key="status"
            ),
            honor_unsubscribe=_parse_verdict(
                compliance_data.get("honorUnsubscribeVerdict"), status_key="status"
            ),
        )

    def create_domain(self, domain_id: str) -> None:
        response = self._request("POST", "/v2/domains", json={"domainId": domain_id})
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: create domain returned status {response.status_code}"
            )

    def get_verification_token(self, domain: str, *, method: str = "TXT") -> str:
        if method not in ("TXT", "CNAME"):
            raise ValueError(f"method must be 'TXT' or 'CNAME', got {method!r}")
        response = self._request(
            "GET",
            f"/v2/domains/{domain}/verificationToken",
            params={"verificationMethod": method},
        )
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: getVerificationToken returned status {response.status_code}"
            )
        payload = _parse_json(response)
        body = require_dict(payload, _PROVIDER, "verificationToken response")
        return require_str(body, "token", _PROVIDER)

    def verify_domain(self, domain: str, *, method: str = "TXT") -> bool:
        if method not in ("TXT", "CNAME"):
            raise ValueError(f"method must be 'TXT' or 'CNAME', got {method!r}")
        response = self._request(
            "POST", f"/v2/domains/{domain}:verify", json={"verificationMethod": method}
        )
        # VerifyDomainResponse has no fields (per the live discovery
        # document) -- success is the status code alone.
        return response.status_code == 200


def _to_api_date(d: date) -> dict[str, int]:
    return {"year": d.year, "month": d.month, "day": d.day}


def _parse_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise MalformedResponseError(f"{_PROVIDER}: response body was not valid JSON") from exc


def _parse_domain_stat(raw_row: object) -> DomainStatRow:
    row = require_dict(raw_row, _PROVIDER, "domainStats row")
    metric_name = require_str(row, "metric", _PROVIDER)
    value = _parse_statistic_value(require_dict(row.get("value", {}), _PROVIDER, "'value'"))
    day, source_day = _parse_date(row.get("date"))
    return DomainStatRow(metric_name=metric_name, day=day, value=value, source_day=source_day)


def _parse_statistic_value(value: dict[str, object]) -> float:
    if "doubleValue" in value:
        raw = value["doubleValue"]
    elif "floatValue" in value:
        raw = value["floatValue"]
    elif "intValue" in value:
        # int64 comes back as a JSON string to avoid precision loss.
        raw = value["intValue"]
        if not isinstance(raw, str):
            raise MalformedResponseError(
                f"{_PROVIDER}: expected 'intValue' to be a string, got {type(raw).__name__}"
            )
        try:
            return float(int(raw))
        except ValueError as exc:
            raise MalformedResponseError(
                f"{_PROVIDER}: 'intValue' is not an integer: {raw!r}"
            ) from exc
    else:
        raise MalformedResponseError(
            f"{_PROVIDER}: StatisticValue has none of doubleValue/floatValue/intValue"
        )
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise MalformedResponseError(
            f"{_PROVIDER}: expected a numeric statistic value, got {type(raw).__name__}"
        )
    return float(raw)


def _parse_date(raw: object) -> tuple[date | None, str | None]:
    if raw is None:
        return None, None
    date_obj = require_dict(raw, _PROVIDER, "'date'")
    year = require_int(date_obj, "year", _PROVIDER)
    month = require_int(date_obj, "month", _PROVIDER)
    day = require_int(date_obj, "day", _PROVIDER)
    source = f"{year:04d}-{month:02d}-{day:02d}"
    if year == 0 or month == 0 or day == 0:
        # A partial Date per the type's own semantics (e.g. year-only) --
        # not representable as a full calendar day.
        return None, source
    return date(year, month, day), source


def _parse_verdict(raw: object, *, status_key: str) -> ComplianceVerdict:
    if raw is None:
        # Absent verdict: unknown, not "compliant." Never coerce silence
        # into a clean bill of health.
        return ComplianceVerdict(status=ComplianceState.STATE_UNSPECIFIED, reason=None)
    verdict = require_dict(raw, _PROVIDER, "verdict")
    status_obj = require_dict(verdict.get(status_key, {}), _PROVIDER, f"verdict '{status_key}'")
    status_raw = status_obj.get("status")
    if not isinstance(status_raw, str):
        raise MalformedResponseError(f"{_PROVIDER}: verdict status missing or not a string")
    try:
        status = ComplianceState[status_raw]
    except KeyError as exc:
        raise MalformedResponseError(
            f"{_PROVIDER}: unknown compliance status {status_raw!r}"
        ) from exc
    reason_raw = verdict.get("reason")
    reason = reason_raw if isinstance(reason_raw, str) and reason_raw else None
    return ComplianceVerdict(status=status, reason=reason)
