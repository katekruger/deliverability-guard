"""The refusal is the product.

Runs entirely in dry-run, needs no credentials, and contacts no real
provider -- it's the same `engine.breaker.evaluate` every real evaluation
goes through, just with a driver that has no capabilities and nothing to
call.

    uv run python examples/demo.py

Shows the exact two cases that are this project's entire argument
(BUILD-PLAN.md §6, docs/statistics.md):

  1. 1 complaint in 50 sends -- a naive fixed-window breaker fires here,
     because 2% is 6.7x Gmail's 0.3% ceiling. The posterior's lower bound
     refuses to.
  2. 40 complaints in 5,000 sends -- real signal. The same posterior
     correctly, confidently trips.
"""

from datetime import UTC, datetime

from deliverability_guard.engine.breaker import DEFAULT_LADDER, BreakerStateStore, evaluate
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.providers.base import (
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
)


class _NullDriver:
    """No capabilities, nothing to call -- makes it obvious this demo can't
    touch a real mailbox even before dry_run=True already guarantees it."""

    name = "demo"
    capabilities: frozenset[Capability] = frozenset()

    def read_mailbox_stats(self, since: object) -> list[MailboxDayStats]:
        return []

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        raise NotImplementedError("the demo never reaches a real action")

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        raise NotImplementedError("the demo never reaches a real action")


def _run(label: str, *, sends: int, complaints: int) -> None:
    mailbox = MailboxRef(provider="demo", mailbox_id="sender@example.com")
    result = evaluate(
        driver=_NullDriver(),
        mailbox=mailbox,
        sends=sends,
        complaints=complaints,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=datetime.now(UTC),
    )

    print(f"\n{label}")
    print(f"  sends={sends}  complaints={complaints}")
    if result.posterior is None or result.lower_bound is None:
        print(f"  data_state={result.data_state.name}  (no posterior computed)")
    else:
        point_estimate = complaints / sends
        print(f"  naive point estimate:          {point_estimate:.4%}")
        print(f"  posterior lower bound (95%):   {result.lower_bound:.4%}")
    print(f"  verdict={result.verdict.name}")


def main() -> None:
    print("deliverability-guard -- dry-run demo. No credentials. No live calls.")
    print("=" * 72)
    _run(
        "Case 1: 1 complaint in 50 sends -- 0.3% of 50 is 0.15 of a message",
        sends=50,
        complaints=1,
    )
    _run(
        "Case 2: 40 complaints in 5,000 sends -- real signal",
        sends=5000,
        complaints=40,
    )
    print("=" * 72)
    print("\nCase 1 refuses to trip. Case 2 does. Same math, same code path.")


if __name__ == "__main__":
    main()
