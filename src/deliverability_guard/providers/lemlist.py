"""lemlist provider driver.

    Base: https://api.lemlist.com/api, HTTP Basic auth (empty username, API
          key as password -- lemlist's own convention, not a project choice)
    GET  /campaigns/{campaignId}/export?type=activities&fileFormat=json
    POST /campaigns/{campaignId}/pause
    POST /campaigns/{campaignId}/start

BUILD-PLAN.md §5's capability matrix: lemlist's pause is documented
idempotent and cheap to integrate, but it is campaign-granularity only --
there is no per-mailbox pause or throttle endpoint, so `capabilities`
excludes both THROTTLE and per-mailbox PAUSE. `pause()` therefore accepts a
`CampaignRef` and returns `unsupported` for a bare `MailboxRef`, the same
pattern `smartlead.py` uses for the same reason.

Read access is the `activities` export, not a per-mailbox daily-stats
endpoint the way Instantly's is -- an activity record is one event (a send,
a bounce, an open) attributed to whichever connected mailbox
("sendUserEmail") sent it. `read_mailbox_stats` aggregates these into daily
per-mailbox counts. WEBHOOKS is deliberately excluded from `capabilities`:
BUILD-PLAN.md §5 flags lemlist's webhook support as "unverified," and this
driver doesn't claim a capability it hasn't confirmed.

Same provenance caveat as `providers/instantly.py`: the endpoint shapes
below are hand-authored from lemlist's public API documentation, not
captured from a live account (this environment has no lemlist
credentials). Verify against a real captured-and-redacted response before
trusting this driver against production traffic -- see
tests/fixtures/lemlist/README.md.
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
    MalformedResponseError,
    unsupported,
)

_BASE_URL = "https://api.lemlist.com/api"
_PROVIDER = "lemlist"

# Activity types that count toward sends/bounces. lemlist's export is a raw
# event log with many other types (opens, replies, clicks, ...) that this
# driver deliberately ignores -- only these two feed the breaker.
_SEND_TYPE = "emailsSent"
_BOUNCE_TYPE = "emailsBounced"


class LemlistDriver:
    """See module docstring."""

    name = _PROVIDER
    capabilities = frozenset({Capability.READ_STATS, Capability.PAUSE})

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

    def _auth(self) -> httpx.BasicAuth:
        # lemlist authenticates via HTTP Basic with an empty username and
        # the API key as the password -- not a project convention, lemlist's.
        return httpx.BasicAuth(username="", password=self._api_key)

    def _request(self, request: Callable[[], httpx.Response]) -> httpx.Response:
        return request_with_retry(request, sleep=self._sleep, rand=self._rand)

    def read_mailbox_stats(self, since: date, *, campaign_id: str) -> list[MailboxDayStats]:
        """Aggregates the campaign's raw activity export into per-mailbox,
        per-day send/bounce counts. `since` filters client-side, same
        rationale as `smartlead.py`: no documented date-range parameter to
        build a fixture-testable request against.
        """
        response = self._request(
            lambda: self._client.get(
                f"/campaigns/{campaign_id}/export",
                params={"type": "activities", "fileFormat": "json"},
                auth=self._auth(),
            )
        )
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: activities export returned status {response.status_code}"
            )
        activities = [_parse_activity(row) for row in _extract_rows(response)]
        return _aggregate_daily(activities, since=since)

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return unsupported(
            Capability.THROTTLE,
            self.name,
            "lemlist has no per-mailbox daily-limit endpoint",
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        if isinstance(target, MailboxRef):
            return unsupported(
                Capability.PAUSE,
                self.name,
                "lemlist has no per-mailbox pause endpoint; pause a CampaignRef instead",
            )
        return self.pause_campaign(target.campaign_id)

    def pause_campaign(self, campaign_id: str) -> ActionResult:
        """lemlist-specific: pause is documented idempotent server-side
        (BUILD-PLAN.md §5), so this driver adds no idempotency guard of its
        own -- repeated calls are safe by the provider's own contract."""
        return self._set_campaign_state(campaign_id, "pause")

    def start_campaign(self, campaign_id: str) -> ActionResult:
        """lemlist-specific, symmetric with `pause_campaign`. Not a generic
        un-pause verb on the base Protocol -- AGENTS.md: never auto-resume
        without a human; this exists for an operator to call deliberately."""
        return self._set_campaign_state(campaign_id, "start")

    def _set_campaign_state(self, campaign_id: str, action: str) -> ActionResult:
        response = self._request(
            lambda: self._client.post(f"/campaigns/{campaign_id}/{action}", auth=self._auth())
        )
        if response.status_code >= 400:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: campaign {action} failed with status {response.status_code}",
                capability=Capability.PAUSE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: campaign {campaign_id} {action}d",
            capability=Capability.PAUSE,
        )


class _Activity:
    __slots__ = ("activity_type", "day", "mailbox_id", "source_day")

    def __init__(self, mailbox_id: str, day: date, source_day: str, activity_type: str) -> None:
        self.mailbox_id = mailbox_id
        self.day = day
        self.source_day = source_day
        self.activity_type = activity_type


def _extract_rows(response: httpx.Response) -> list[object]:
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise MalformedResponseError(f"{_PROVIDER}: response body was not valid JSON") from exc
    if isinstance(payload, list):
        return cast(list[object], payload)
    body = require_dict(payload, _PROVIDER, "response body")
    return require_list(body.get("activities"), _PROVIDER, "'activities'")


def _parse_activity(raw_row: object) -> _Activity:
    row = require_dict(raw_row, _PROVIDER, "activity row")
    mailbox_id = require_str(row, "sendUserEmail", _PROVIDER)
    raw_date = require_str(row, "date", _PROVIDER)
    activity_type = require_str(row, "type", _PROVIDER)
    return _Activity(
        mailbox_id=mailbox_id,
        day=normalize_to_utc_date(raw_date, _PROVIDER),
        source_day=raw_date,
        activity_type=activity_type,
    )


def _aggregate_daily(activities: list[_Activity], *, since: date) -> list[MailboxDayStats]:
    totals: dict[tuple[str, date], list[int]] = {}
    source_days: dict[tuple[str, date], str] = {}
    for activity in activities:
        if activity.day < since:
            continue
        if activity.activity_type not in (_SEND_TYPE, _BOUNCE_TYPE):
            continue
        key = (activity.mailbox_id, activity.day)
        counts = totals.setdefault(key, [0, 0])
        if activity.activity_type == _SEND_TYPE:
            counts[0] += 1
        else:
            counts[1] += 1
        source_days[key] = activity.source_day

    return [
        MailboxDayStats(
            mailbox=MailboxRef(provider=_PROVIDER, mailbox_id=mailbox_id),
            day=day,
            sends=counts[0],
            bounces=counts[1],
            source_day=source_days[(mailbox_id, day)],
        )
        for (mailbox_id, day), counts in sorted(totals.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]
