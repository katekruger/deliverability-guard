"""The provider driver interface. Capability declaration is the whole design.

Verified reality (BUILD-PLAN.md §5): of nine surveyed sequencer platforms,
two cannot pause anything at all. Amplemarket has no status-change API of any
kind -- sequences carry paused/pausing states, but only the web app can set
them. Salesloft's cadence pause appears to be UI-only. Postmark and SendGrid
can report stats but have no pause primitive at all. An interface that
assumed every provider could pause would already be wrong for nearly half of
what was surveyed.

Every driver therefore DECLARES what it can do via `capabilities`, and every
driver's `pause`/`throttle` methods exist and are always callable -- for a
capability (or a specific target) a driver doesn't support, they return an
`ActionResult` with `outcome=ActionOutcome.UNSUPPORTED` rather than raising.
The breaker (Prompt 3) degrades to alert-only in that case, and the decision
log records exactly why nothing was attempted. Silently no-oping is the one
behavior this design exists to rule out.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, auto
from typing import Protocol

_EMPTY_MAPPING: Mapping[str, object] = {}


class Capability(Enum):
    READ_STATS = auto()
    THROTTLE = auto()
    PAUSE = auto()
    WEBHOOKS = auto()


class ProviderError(Exception):
    """Base class for provider driver errors."""


class RateLimitExceededError(ProviderError):
    """A provider kept returning 429 past the retry budget.

    This is NOT evidence about mailbox health. A caller that catches this
    must back off and re-evaluate later -- a rate limit must never be read
    as, or silently folded into, a reputation breach.
    """


class MalformedResponseError(ProviderError):
    """A provider's response couldn't be parsed into our types.

    Kept distinct from `RateLimitExceededError` so callers -- and the
    decision log -- can tell "the provider is throttling us" apart from
    "the provider sent us something we don't understand." Those call for
    different responses: back off vs. alert a human.
    """


@dataclass(frozen=True, slots=True)
class MailboxRef:
    provider: str
    mailbox_id: str


@dataclass(frozen=True, slots=True)
class CampaignRef:
    provider: str
    campaign_id: str


class MailboxStatus(Enum):
    """Whether a mailbox's stats for a given day reflect normal sending.

    A DISCONNECTED day is an outage, not a reputation signal. Reading it as
    "0 sends, 0 bounces, therefore healthy" is exactly the missing-data-as-
    zero coercion AGENTS.md prohibits -- callers must check `status` before
    drawing any conclusion from `sends`/`bounces` on that day, and an outage
    demands a different response (fix the connection) than a reputation
    breach (throttle or pause) does.
    """

    ACTIVE = auto()
    DISCONNECTED = auto()


@dataclass(frozen=True, slots=True)
class MailboxDayStats:
    """One mailbox's stats for one UTC calendar day.

    `day` is always normalized to UTC at the driver boundary. Providers
    report in whatever timezone their account or API defaults to, and if
    drivers don't agree on what "a day" means, daily aggregation across a
    timezone boundary is a real off-by-one-day bug. `source_day` preserves
    the provider's own original reported value, unmodified, for audit --
    normalization is not supposed to be a black box.
    """

    mailbox: MailboxRef
    day: date
    sends: int
    bounces: int
    status: MailboxStatus = MailboxStatus.ACTIVE
    source_day: str = ""
    current_daily_limit: int | None = None
    """The mailbox's sending cap as of this day, if the provider's response
    happened to include one -- `None` when unknown, never coerced to a
    guess. This is what `engine.breaker.evaluate`'s `current_daily_limit`
    needs to compute a THROTTLE action (CLOSE-1/CLOSE-3a): without it, a
    THROTTLE verdict can never actually reduce a mailbox's limit, only
    report itself as `UNSUPPORTED`. `loops.fast.aggregate_mailbox_stats`
    takes the most recent day's value per mailbox, since a daily limit is a
    point-in-time setting, not something to sum across days like sends."""

    def __post_init__(self) -> None:
        if self.sends < 0:
            raise ValueError(f"sends must be >= 0, got {self.sends}")
        if self.current_daily_limit is not None and self.current_daily_limit < 0:
            raise ValueError(
                f"current_daily_limit must be >= 0 or None, got {self.current_daily_limit}"
            )
        if self.bounces < 0:
            raise ValueError(f"bounces must be >= 0, got {self.bounces}")


class ActionOutcome(Enum):
    PERFORMED = auto()
    FAILED = auto()
    UNSUPPORTED = auto()
    # Audit-log-only: what a dry-run action would have done, distinct from
    # PERFORMED so a persisted record never claims a real provider call
    # happened when it didn't (see `audit.log.DecisionRecord.from_evaluation`).
    # No `ProviderDriver`/`DryRunDriver` implementation ever returns this --
    # `BreakerEvaluation.action.outcome` stays PERFORMED for a dry-run
    # action, which is what AGENTS.md's "dry-run must produce decisions
    # identical to the live path" requires at the engine level. Only the
    # decision log, whose job is to tell a human/replay what actually
    # happened in the world, distinguishes the two.
    DRY_RUN = auto()


@dataclass(frozen=True, slots=True)
class ActionResult:
    outcome: ActionOutcome
    detail: str
    capability: Capability


def unsupported(capability: Capability, provider: str, reason: str) -> ActionResult:
    """Build the `ActionResult` a driver returns for something it can't do.

    Every driver's `pause`/`throttle` must be reachable and must return this
    rather than raising, so calling code -- and the decision log -- can
    treat "not supported" the same way as every other outcome, instead of
    needing a provider-specific try/except at every call site.
    """
    return ActionResult(
        outcome=ActionOutcome.UNSUPPORTED,
        detail=f"{provider}: {capability.name} is not supported -- {reason}",
        capability=capability,
    )


class ProviderDriver(Protocol):
    # Declared as read-only properties, not plain mutable attributes: a
    # Protocol's plain attributes are invariant, which would reject any
    # implementation (like providers/dry_run.py's DryRunDriver) that
    # exposes these as `@property` instead of a bare class attribute, even
    # though both satisfy "read `driver.name` and get a str" identically.
    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[Capability]: ...

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]: ...
    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult: ...
    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult: ...


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """A single provider webhook delivery, normalized.

    `occurred_at` is the event's own timestamp as reported by the provider,
    normalized to UTC -- NOT the time it was received. Event ordering must
    use this, never arrival order (see `order_events`), because providers
    redeliver, and redeliveries do not arrive in the order things happened.
    """

    event_id: str
    provider: str
    event_type: str
    occurred_at: datetime
    mailbox: MailboxRef | None = None
    campaign: CampaignRef | None = None
    raw: Mapping[str, object] = field(default_factory=lambda: _EMPTY_MAPPING)


class WebhookLedger:
    """Tracks which webhook event ids have already been processed.

    Providers redeliver webhooks -- that's a normal, expected consequence of
    an at-least-once delivery guarantee, not an error condition. Counting
    the same complaint or bounce twice because of a redelivery would
    directly corrupt the posterior in engine/posterior.py, so idempotency by
    event id is enforced here, once, rather than trusted to every call site.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def accept(self, event: WebhookEvent) -> bool:
        """True the first time this event id is seen; False on any redelivery."""
        if event.event_id in self._seen:
            return False
        self._seen.add(event.event_id)
        return True


def order_events(events: Iterable[WebhookEvent]) -> list[WebhookEvent]:
    """Sort webhook events by when they happened, not the order they arrived in."""
    return sorted(events, key=lambda e: e.occurred_at)
