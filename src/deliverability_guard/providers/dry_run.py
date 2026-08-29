"""The dry-run provider decorator.

AGENTS.md: dry-run is the default, and it must produce decisions IDENTICAL
to the live path -- the only allowed difference is that the provider call
itself doesn't happen. This wraps any `ProviderDriver` and intercepts
`throttle()`/`pause()` to report what WOULD have happened without touching
the real provider, while `read_mailbox_stats()` passes straight through
unchanged: reading is never dangerous, and dry-run still needs real data to
decide what it would do.

Nowhere else in this codebase branches on a `dry_run` flag to decide WHAT to
do -- `engine/breaker.py` runs the exact same evaluation logic either way,
and only the driver object handed to it differs. This is that mechanism.
"""

from dataclasses import dataclass
from datetime import date

from deliverability_guard.providers.base import (
    ActionOutcome,
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
    ProviderDriver,
    unsupported,
)


@dataclass(frozen=True, slots=True)
class DryRunDriver:
    """Wraps `inner`. `capabilities` and `name` pass through unchanged --
    dry-run doesn't change what a provider is capable of, only whether an
    action is actually sent."""

    inner: ProviderDriver

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.inner.capabilities

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        return self.inner.read_mailbox_stats(since)

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        if Capability.THROTTLE not in self.inner.capabilities:
            return unsupported(
                Capability.THROTTLE, self.inner.name, "not supported by this provider"
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"[DRY RUN] would throttle {mailbox_id} to {daily_limit}/day",
            capability=Capability.THROTTLE,
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        if Capability.PAUSE not in self.inner.capabilities:
            return unsupported(Capability.PAUSE, self.inner.name, "not supported by this provider")
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"[DRY RUN] would pause {target}",
            capability=Capability.PAUSE,
        )
