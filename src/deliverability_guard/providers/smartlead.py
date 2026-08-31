"""Smartlead provider driver -- proves the THROTTLE path.

    Base: https://server.smartlead.ai/api/v1
    GET   /campaigns/{id}/statistics
    PATCH /campaigns/{id}/status         (START | PAUSED | STOPPED)
    POST  /email-accounts/{id}           (update daily limit)  <- THE THROTTLE PRIMITIVE

The daily-limit endpoint is the most underrated one in this whole space: it
is the difference between a circuit breaker and a kill switch (BUILD-PLAN.md
§5). `capabilities` therefore includes THROTTLE.

`capabilities` deliberately EXCLUDES PAUSE, even though Smartlead's
campaign-status endpoint can technically pause a whole campaign. This
project's `pause()` contract is per-mailbox (BUILD-PLAN.md §2: "the
breaker's unit is the mailbox"), and pausing an entire campaign to handle
one bad mailbox is a disproportionate action this driver refuses to expose
as if it were equivalent to a real per-mailbox pause. The campaign-status
endpoint is still implemented, as `pause_campaign()` / `activate_campaign()`
-- Smartlead-specific methods beyond the base Protocol -- for callers that
explicitly want campaign-wide action, not as something `pause()` silently
does instead of what was asked.

SECURITY: Smartlead authenticates via `?api_key=` in the query string, not a
header. That value lands in server access logs, any proxy in front of this
process, and `Referer` headers on any request the response might trigger.
**Nothing in this module logs or raises with a full request URL** -- every
error message here is built from a hardcoded path description, never from
`response.request.url` or `str(response.url)`. Keep it that way in any
future edit to this file. See SECURITY.md.

`SmartleadDriver.read_mailbox_stats` takes a required `campaign_id` keyword
argument, which the base `providers.base.ProviderDriver` Protocol's
`read_mailbox_stats(self, since)` has no room for -- Smartlead's statistics
endpoint really is per-campaign, not global (see the module docstring
above), so there is no single implementation of the generic method that
would be correct without a campaign pinned somewhere. `SmartleadCampaignDriver`
below is that pinning: it adapts a `SmartleadDriver` plus one campaign id
into something that satisfies `ProviderDriver` exactly, for `cli.build_driver`
to construct (CLOSE-5a). Every other method passes straight through to
`inner`, unchanged.
"""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
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

_BASE_URL = "https://server.smartlead.ai/api/v1"
_PROVIDER = "smartlead"


class SmartleadDriver:
    """See module docstring."""

    name = _PROVIDER
    capabilities = frozenset({Capability.READ_STATS, Capability.THROTTLE, Capability.WEBHOOKS})

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

    def _auth_params(self) -> dict[str, str]:
        return {"api_key": self._api_key}

    def _request(self, request: Callable[[], httpx.Response]) -> httpx.Response:
        return request_with_retry(request, sleep=self._sleep, rand=self._rand)

    def read_mailbox_stats(self, since: date, *, campaign_id: str) -> list[MailboxDayStats]:
        """Smartlead's statistics endpoint is per-campaign, not a global
        per-mailbox feed (BUILD-PLAN.md §5's capability matrix: "per-
        campaign", unlike Instantly's per-mailbox analytics) -- callers
        supply the campaign to read. `since` filters rows client-side,
        since the endpoint has no documented date-range parameter to rely
        on for a fixture-testable contract.
        """
        response = self._request(
            lambda: self._client.get(
                f"/campaigns/{campaign_id}/statistics", params=self._auth_params()
            )
        )
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: campaign statistics returned status {response.status_code}"
            )
        rows = _extract_rows(response)
        stats = [_parse_statistics_row(row) for row in rows]
        return [s for s in stats if s.day >= since]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        if daily_limit < 0:
            raise ValueError(f"daily_limit must be >= 0, got {daily_limit}")
        response = self._request(
            lambda: self._client.post(
                f"/email-accounts/{mailbox_id}",
                params=self._auth_params(),
                json={"message_per_day": daily_limit},
            )
        )
        if response.status_code >= 400:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: throttle failed with status {response.status_code}",
                capability=Capability.THROTTLE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: throttled mailbox to {daily_limit}/day",
            capability=Capability.THROTTLE,
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        if isinstance(target, MailboxRef):
            return unsupported(
                Capability.PAUSE,
                self.name,
                "Smartlead has no per-mailbox pause endpoint; use throttle(), "
                "or pause a CampaignRef via pause_campaign()",
            )
        return self.pause_campaign(target.campaign_id)

    def pause_campaign(self, campaign_id: str) -> ActionResult:
        """Smartlead-specific: pauses an entire campaign. Not reachable
        through the generic `pause()` for a MailboxRef -- see module
        docstring for why."""
        return self._set_campaign_status(campaign_id, "PAUSED")

    def activate_campaign(self, campaign_id: str) -> ActionResult:
        """Smartlead-specific, symmetric with `pause_campaign`. Not a
        generic un-pause verb on the base Protocol -- AGENTS.md: never
        auto-resume without a human; this exists for an operator to call
        deliberately."""
        return self._set_campaign_status(campaign_id, "START")

    def _set_campaign_status(self, campaign_id: str, status: str) -> ActionResult:
        response = self._request(
            lambda: self._client.patch(
                f"/campaigns/{campaign_id}/status",
                params=self._auth_params(),
                json={"status": status},
            )
        )
        if response.status_code >= 400:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: campaign status update to {status} failed "
                f"with status {response.status_code}",
                capability=Capability.PAUSE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: campaign {campaign_id} set to {status}",
            capability=Capability.PAUSE,
        )


@dataclass(frozen=True, slots=True)
class SmartleadCampaignDriver:
    """See module docstring: adapts `SmartleadDriver` to the generic
    `ProviderDriver` Protocol by pinning `read_mailbox_stats` to one
    campaign id. `throttle`/`pause` pass straight through to `inner`."""

    inner: SmartleadDriver
    campaign_id: str

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.inner.capabilities

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        return self.inner.read_mailbox_stats(since, campaign_id=self.campaign_id)

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return self.inner.throttle(mailbox_id, daily_limit)

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        return self.inner.pause(target)


def _extract_rows(response: httpx.Response) -> list[object]:
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise MalformedResponseError(f"{_PROVIDER}: response body was not valid JSON") from exc
    if isinstance(payload, list):
        return cast(list[object], payload)
    body = require_dict(payload, _PROVIDER, "response body")
    return require_list(body.get("data"), _PROVIDER, "'data'")


def _parse_statistics_row(raw_row: object) -> MailboxDayStats:
    row = require_dict(raw_row, _PROVIDER, "statistics row")
    mailbox_id = require_str(row, "from_email", _PROVIDER)
    raw_date = require_str(row, "sent_date", _PROVIDER)
    sends = require_int(row, "sent_count", _PROVIDER)
    bounces = require_int(row, "bounce_count", _PROVIDER)
    disconnected_raw = row.get("email_account_disconnected")
    status = (
        MailboxStatus.DISCONNECTED
        if isinstance(disconnected_raw, bool) and disconnected_raw
        else MailboxStatus.ACTIVE
    )
    return MailboxDayStats(
        mailbox=MailboxRef(provider=_PROVIDER, mailbox_id=mailbox_id),
        day=normalize_to_utc_date(raw_date, _PROVIDER),
        sends=sends,
        bounces=bounces,
        status=status,
        source_day=raw_date,
    )
