"""Instantly provider driver -- the reference implementation.

Instantly is the only vendor surveyed exposing BOTH per-mailbox daily bounce
data AND per-mailbox pause (BUILD-PLAN.md §5) -- the minimum viable
substrate for a circuit breaker, which is why it's first and why every other
driver in this project follows its shape.

    Base: https://api.instantly.ai, Bearer auth
    POST /api/v2/accounts/{email}/pause
    GET  /api/v2/accounts/analytics/daily      (sent, bounced, per mailbox per day)
    POST /api/v2/accounts/warmup-analytics     (sent, landed_inbox, landed_spam, health_score)
    POST /api/v2/campaigns/{id}/pause
    POST /api/v2/campaigns/{id}/activate

Rate limits are not publicly documented. This driver assumes 429s can happen
at any time and retries with exponential backoff and full jitter (see
providers/_retry.py). IMPORTANT: the retry defaults below are conservative
guesses, not a measurement -- this project does not currently have a live
Instantly account to measure against. The first time this runs against real
traffic, replace this paragraph with actual observed behavior and the date
it was measured, per AGENTS.md's pinned-linters-style rationale: an
undocumented assumption silently goes stale, a dated one at least tells you
when to be suspicious of it.

Likewise: the response shapes below (and the fixtures in
tests/fixtures/instantly/) are hand-authored from Instantly's public API
documentation and general REST conventions, NOT captured from a live
account, for the same reason -- see tests/fixtures/instantly/README.md. Ship
this driver, but verify its parsing against a real response before trusting
it against production traffic.
"""

import random
import time
from collections.abc import Callable
from datetime import date
from typing import cast

import httpx

from deliverability_guard.providers._parsing import (
    normalize_to_utc_date,
    require_dict,
    require_int,
    require_list,
    require_str,
)
from deliverability_guard.providers._retry import request_with_retry
from deliverability_guard.providers.base import (
    ActionOutcome,
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
    MailboxStatus,
    MalformedResponseError,
    unsupported,
)

_BASE_URL = "https://api.instantly.ai"
_PROVIDER = "instantly"


class InstantlyDriver:
    """See module docstring. `capabilities` deliberately excludes THROTTLE:
    Instantly has no per-mailbox daily-limit endpoint, only per-mailbox
    pause -- see providers/smartlead.py for the driver that proves the
    throttle path instead."""

    name = _PROVIDER
    capabilities = frozenset({Capability.READ_STATS, Capability.PAUSE, Capability.WEBHOOKS})

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: random.Random | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(base_url=_BASE_URL, timeout=30.0)
        self._sleep = sleep
        self._rand = rand

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _request(self, request: Callable[[], httpx.Response]) -> httpx.Response:
        return request_with_retry(request, sleep=self._sleep, rand=self._rand)

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        response = self._request(
            lambda: self._client.get(
                "/api/v2/accounts/analytics/daily",
                params={"start_date": since.isoformat()},
                headers=self._headers(),
            )
        )
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: analytics/daily returned status {response.status_code}"
            )
        rows = _extract_rows(response, key="data")
        return [_parse_daily_row(row) for row in rows]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return unsupported(
            Capability.THROTTLE,
            self.name,
            "Instantly has no per-mailbox daily-limit endpoint; see SmartleadDriver",
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        if isinstance(target, MailboxRef):
            path = f"/api/v2/accounts/{target.mailbox_id}/pause"
        else:
            path = f"/api/v2/campaigns/{target.campaign_id}/pause"
        response = self._request(lambda: self._client.post(path, headers=self._headers()))
        if response.status_code >= 400:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: pause failed with status {response.status_code}",
                capability=Capability.PAUSE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: paused {target}",
            capability=Capability.PAUSE,
        )

    def activate_campaign(self, campaign_id: str) -> ActionResult:
        """Not part of the base Protocol -- there's no generic "un-pause"
        verb in ProviderDriver (AGENTS.md: never auto-resume without a
        human, see the ladder in Prompt 3), but Instantly's own
        campaign-activate endpoint is worth exposing directly for a human
        or an operator tool to call."""
        response = self._request(
            lambda: self._client.post(
                f"/api/v2/campaigns/{campaign_id}/activate", headers=self._headers()
            )
        )
        if response.status_code >= 400:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: activate failed with status {response.status_code}",
                capability=Capability.PAUSE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: activated campaign {campaign_id}",
            capability=Capability.PAUSE,
        )


def _extract_rows(response: httpx.Response, *, key: str) -> list[object]:
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise MalformedResponseError(f"{_PROVIDER}: response body was not valid JSON") from exc
    if isinstance(payload, list):
        return cast(list[object], payload)
    body = require_dict(payload, _PROVIDER, "response body")
    return require_list(body.get(key), _PROVIDER, f"'{key}'")


def _parse_daily_row(raw_row: object) -> MailboxDayStats:
    row = require_dict(raw_row, _PROVIDER, "daily row")
    email = require_str(row, "email", _PROVIDER)
    raw_date = require_str(row, "date", _PROVIDER)
    sends = require_int(row, "sent", _PROVIDER)
    bounces = require_int(row, "bounced", _PROVIDER)
    status_raw = row.get("status")
    status = (
        MailboxStatus.DISCONNECTED
        if isinstance(status_raw, str) and status_raw.lower() == "disconnected"
        else MailboxStatus.ACTIVE
    )
    return MailboxDayStats(
        mailbox=MailboxRef(provider=_PROVIDER, mailbox_id=email),
        day=normalize_to_utc_date(raw_date, _PROVIDER),
        sends=sends,
        bounces=bounces,
        status=status,
        source_day=raw_date,
    )
