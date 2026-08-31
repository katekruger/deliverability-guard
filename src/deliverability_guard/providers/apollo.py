"""Apollo.io provider driver.

    Base: https://api.apollo.io/v1, `X-Api-Key` header auth
    GET  /emailer_campaigns/{campaignId}/daily_stats
    POST /emailer_campaigns/{campaignId}/abort
    POST /emailer_campaigns/{campaignId}/activate
    GET  /email_accounts

BUILD-PLAN.md §5's capability matrix: Apollo's per-mailbox pause/throttle
column is "list only" -- `GET /email_accounts` can enumerate connected
mailboxes, but there is no endpoint to act on one individually. The only
write primitive is sequence-level: `/abort`, named explicitly in
BUILD-PLAN.md's own research rather than guessed here. `capabilities`
therefore has PAUSE (campaign-only) and READ_STATS, but neither THROTTLE
nor WEBHOOKS -- Apollo has no daily-limit endpoint at any granularity, and
its webhook support is "polling only" (BUILD-PLAN.md §5), i.e. not a push
capability this driver can claim.

BUILD-PLAN.md flags Apollo's resume semantics as explicitly unverified
("verify resume semantics"). `activate_campaign` below calls the mirror
endpoint of `/abort`, but whether that reliably restores a sequence to its
prior state (vs. e.g. resetting contact-level progress) has not been
confirmed against a live account -- treat it with more suspicion than
`pause`/`abort` until verified.

Same provenance caveat as `providers/instantly.py`: endpoint shapes are
hand-authored from Apollo's public API documentation, not captured from a
live account. Verify against a real captured-and-redacted response before
trusting this driver against production traffic -- see
tests/fixtures/apollo/README.md.
"""

import random
import time
from collections.abc import Callable
from datetime import date

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
    MalformedResponseError,
    unsupported,
)

_BASE_URL = "https://api.apollo.io/v1"
_PROVIDER = "apollo"


class ApolloDriver:
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

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}

    def _request(self, request: Callable[[], httpx.Response]) -> httpx.Response:
        return request_with_retry(request, sleep=self._sleep, rand=self._rand)

    def read_mailbox_stats(self, since: date, *, campaign_id: str) -> list[MailboxDayStats]:
        """Per-campaign, like `smartlead.py` and `lemlist.py` -- Apollo has
        no global per-mailbox feed. `since` filters client-side, no
        documented date-range parameter to build a fixture-testable
        request against."""
        response = self._request(
            lambda: self._client.get(
                f"/emailer_campaigns/{campaign_id}/daily_stats", headers=self._headers()
            )
        )
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: daily_stats returned status {response.status_code}"
            )
        rows = _extract_rows(response)
        stats = [_parse_daily_row(row) for row in rows]
        return [s for s in stats if s.day >= since]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return unsupported(
            Capability.THROTTLE,
            self.name,
            "Apollo has no daily-limit endpoint at any granularity",
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        if isinstance(target, MailboxRef):
            return unsupported(
                Capability.PAUSE,
                self.name,
                "Apollo can only list email accounts, not pause one individually "
                "-- see list_email_accounts()",
            )
        return self.abort_sequence(target.campaign_id)

    def abort_sequence(self, campaign_id: str) -> ActionResult:
        """Apollo-specific name matching its own API (`/abort`), rather than
        a generic `pause_campaign` -- BUILD-PLAN.md's own research names
        this endpoint directly."""
        response = self._request(
            lambda: self._client.post(
                f"/emailer_campaigns/{campaign_id}/abort", headers=self._headers()
            )
        )
        if response.status_code >= 400:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: abort failed with status {response.status_code}",
                capability=Capability.PAUSE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: sequence {campaign_id} aborted",
            capability=Capability.PAUSE,
        )

    def activate_campaign(self, campaign_id: str) -> ActionResult:
        """Apollo-specific, symmetric with `abort_sequence`. UNVERIFIED
        resume semantics (BUILD-PLAN.md §5) -- confirm this actually
        restores prior sequence state, not just a superficially-active
        status, before relying on it. Not a generic un-pause verb on the
        base Protocol either way -- AGENTS.md: never auto-resume without a
        human; this exists for an operator to call deliberately."""
        response = self._request(
            lambda: self._client.post(
                f"/emailer_campaigns/{campaign_id}/activate", headers=self._headers()
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
            detail=f"{_PROVIDER}: sequence {campaign_id} activated (resume semantics unverified)",
            capability=Capability.PAUSE,
        )

    def list_email_accounts(self) -> list[MailboxRef]:
        """The "list only" half of Apollo's mailbox capability (BUILD-PLAN.md
        §5): enumerate connected mailboxes. There is no corresponding
        per-mailbox action -- this exists so a caller can at least discover
        which mailboxes exist, not to feed `pause()`/`throttle()`, which
        remain unsupported for any individual `MailboxRef` on this driver.
        """
        response = self._request(
            lambda: self._client.get("/email_accounts", headers=self._headers())
        )
        if response.status_code != 200:
            raise MalformedResponseError(
                f"{_PROVIDER}: email_accounts returned status {response.status_code}"
            )
        body = require_dict(_parse_json(response), _PROVIDER, "response body")
        rows = require_list(body.get("email_accounts"), _PROVIDER, "'email_accounts'")
        return [
            MailboxRef(
                provider=_PROVIDER,
                mailbox_id=require_str(
                    require_dict(row, _PROVIDER, "email account"), "email", _PROVIDER
                ),
            )
            for row in rows
        ]


def _parse_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise MalformedResponseError(f"{_PROVIDER}: response body was not valid JSON") from exc


def _extract_rows(response: httpx.Response) -> list[object]:
    body = require_dict(_parse_json(response), _PROVIDER, "response body")
    return require_list(body.get("daily_stats"), _PROVIDER, "'daily_stats'")


def _parse_daily_row(raw_row: object) -> MailboxDayStats:
    row = require_dict(raw_row, _PROVIDER, "daily stats row")
    mailbox_id = require_str(row, "sender_email", _PROVIDER)
    raw_date = require_str(row, "date", _PROVIDER)
    sends = require_int(row, "emails_sent", _PROVIDER)
    bounces = require_int(row, "emails_bounced", _PROVIDER)
    return MailboxDayStats(
        mailbox=MailboxRef(provider=_PROVIDER, mailbox_id=mailbox_id),
        day=normalize_to_utc_date(raw_date, _PROVIDER),
        sends=sends,
        bounces=bounces,
        source_day=raw_date,
    )
