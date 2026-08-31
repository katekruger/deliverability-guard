"""Tests for providers/base.py: capability degradation, webhook idempotency
and ordering, and MailboxDayStats validation."""

from datetime import UTC, datetime

import pytest

from deliverability_guard.providers.base import (
    ActionOutcome,
    Capability,
    MailboxDayStats,
    MailboxRef,
    WebhookEvent,
    WebhookLedger,
    order_events,
    unsupported,
)


def test_unsupported_returns_a_structured_result_not_a_raise() -> None:
    """The whole point of `unsupported`: calling code never needs a
    try/except to find out a capability isn't there."""
    result = unsupported(Capability.PAUSE, "amplemarket", "no status-change API exists")
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.PAUSE
    assert "amplemarket" in result.detail
    assert "no status-change API exists" in result.detail


def test_mailbox_day_stats_rejects_negative_sends() -> None:
    with pytest.raises(ValueError, match="sends"):
        MailboxDayStats(
            mailbox=MailboxRef(provider="x", mailbox_id="a@example.com"),
            day=datetime(2026, 1, 1, tzinfo=UTC).date(),
            sends=-1,
            bounces=0,
        )


def test_mailbox_day_stats_rejects_negative_bounces() -> None:
    with pytest.raises(ValueError, match="bounces"):
        MailboxDayStats(
            mailbox=MailboxRef(provider="x", mailbox_id="a@example.com"),
            day=datetime(2026, 1, 1, tzinfo=UTC).date(),
            sends=5,
            bounces=-1,
        )


def test_mailbox_day_stats_rejects_a_negative_current_daily_limit() -> None:
    with pytest.raises(ValueError, match="current_daily_limit"):
        MailboxDayStats(
            mailbox=MailboxRef(provider="x", mailbox_id="a@example.com"),
            day=datetime(2026, 1, 1, tzinfo=UTC).date(),
            sends=5,
            bounces=0,
            current_daily_limit=-1,
        )


def _event(event_id: str, occurred_at: datetime) -> WebhookEvent:
    return WebhookEvent(
        event_id=event_id, provider="instantly", event_type="bounce", occurred_at=occurred_at
    )


def test_webhook_ledger_accepts_a_new_event_id() -> None:
    ledger = WebhookLedger()
    assert ledger.accept(_event("evt-1", datetime(2026, 1, 1, tzinfo=UTC))) is True


def test_webhook_ledger_rejects_a_redelivered_event_id() -> None:
    """Duplicate webhook delivery -> idempotent by event id."""
    ledger = WebhookLedger()
    event = _event("evt-1", datetime(2026, 1, 1, tzinfo=UTC))
    assert ledger.accept(event) is True
    assert ledger.accept(event) is False
    # Redelivered with a different in-memory object but the same id: still rejected.
    assert ledger.accept(_event("evt-1", datetime(2026, 1, 2, tzinfo=UTC))) is False


def test_order_events_sorts_by_occurred_at_not_arrival_order() -> None:
    """Out-of-order webhook delivery -> order by event timestamp, not arrival."""
    late_arriving_but_earlier_event = _event("evt-2", datetime(2026, 1, 1, tzinfo=UTC))
    early_arriving_but_later_event = _event("evt-1", datetime(2026, 1, 2, tzinfo=UTC))
    # Arrival order: evt-1 first, evt-2 second.
    arrival_order = [early_arriving_but_later_event, late_arriving_but_earlier_event]
    assert order_events(arrival_order) == [
        late_arriving_but_earlier_event,
        early_arriving_but_later_event,
    ]
