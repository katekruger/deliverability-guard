"""A configurable ProviderDriver test double. No live calls, ever."""

from dataclasses import dataclass, field
from datetime import date

from deliverability_guard.providers.base import (
    ActionOutcome,
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
    unsupported,
)

_ALL_CAPABILITIES = frozenset(
    {Capability.READ_STATS, Capability.THROTTLE, Capability.PAUSE, Capability.WEBHOOKS}
)


def _no_stats() -> list[MailboxDayStats]:
    return []


def _no_pause_calls() -> list[MailboxRef | CampaignRef]:
    return []


def _no_throttle_calls() -> list[tuple[str, int]]:
    return []


def _no_read_calls() -> list[date]:
    return []


@dataclass
class FakeDriver:
    name: str = "fake"
    capabilities: frozenset[Capability] = field(default_factory=lambda: _ALL_CAPABILITIES)
    pause_outcome: ActionOutcome = ActionOutcome.PERFORMED
    throttle_outcome: ActionOutcome = ActionOutcome.PERFORMED
    raise_on_pause: Exception | None = None
    stats_to_return: list[MailboxDayStats] = field(default_factory=_no_stats)

    pause_calls: list[MailboxRef | CampaignRef] = field(default_factory=_no_pause_calls)
    throttle_calls: list[tuple[str, int]] = field(default_factory=_no_throttle_calls)
    read_calls: list[date] = field(default_factory=_no_read_calls)

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        self.read_calls.append(since)
        return self.stats_to_return

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        self.throttle_calls.append((mailbox_id, daily_limit))
        if Capability.THROTTLE not in self.capabilities:
            return unsupported(Capability.THROTTLE, self.name, "fake: not supported")
        if self.throttle_outcome is ActionOutcome.UNSUPPORTED:
            return unsupported(Capability.THROTTLE, self.name, "fake: forced unsupported")
        ok = self.throttle_outcome is ActionOutcome.PERFORMED
        detail = "fake: throttled" if ok else "fake: throttle failed"
        return ActionResult(
            outcome=self.throttle_outcome, detail=detail, capability=Capability.THROTTLE
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        self.pause_calls.append(target)
        if self.raise_on_pause is not None:
            raise self.raise_on_pause
        if Capability.PAUSE not in self.capabilities:
            return unsupported(Capability.PAUSE, self.name, "fake: not supported")
        if self.pause_outcome is ActionOutcome.UNSUPPORTED:
            return unsupported(Capability.PAUSE, self.name, "fake: forced unsupported")
        ok = self.pause_outcome is ActionOutcome.PERFORMED
        detail = "fake: paused" if ok else "fake: pause failed"
        return ActionResult(outcome=self.pause_outcome, detail=detail, capability=Capability.PAUSE)
