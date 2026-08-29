"""Tests for providers/dry_run.py."""

from datetime import date

from deliverability_guard.providers.base import (
    ActionOutcome,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
)
from deliverability_guard.providers.dry_run import DryRunDriver
from fixtures.fake_driver import FakeDriver

_MAILBOX = MailboxRef(provider="fake", mailbox_id="a@example.com")


def test_read_mailbox_stats_passes_through_unchanged() -> None:
    stats = [MailboxDayStats(mailbox=_MAILBOX, day=date(2026, 1, 1), sends=10, bounces=1)]
    inner = FakeDriver(stats_to_return=stats)
    dry = DryRunDriver(inner=inner)
    assert dry.read_mailbox_stats(since=date(2026, 1, 1)) == stats


def test_throttle_does_not_call_the_real_driver() -> None:
    inner = FakeDriver()
    dry = DryRunDriver(inner=inner)
    result = dry.throttle("a@example.com", 25)
    assert result.outcome == ActionOutcome.PERFORMED
    assert "[DRY RUN]" in result.detail
    assert inner.throttle_calls == []  # the real driver's throttle() was never invoked


def test_pause_does_not_call_the_real_driver() -> None:
    inner = FakeDriver()
    dry = DryRunDriver(inner=inner)
    result = dry.pause(_MAILBOX)
    assert result.outcome == ActionOutcome.PERFORMED
    assert "[DRY RUN]" in result.detail
    assert inner.pause_calls == []


def test_throttle_reports_unsupported_when_inner_lacks_the_capability() -> None:
    inner = FakeDriver(capabilities=frozenset({Capability.READ_STATS}))
    dry = DryRunDriver(inner=inner)
    result = dry.throttle("a@example.com", 25)
    assert result.outcome == ActionOutcome.UNSUPPORTED


def test_pause_reports_unsupported_when_inner_lacks_the_capability() -> None:
    inner = FakeDriver(capabilities=frozenset({Capability.READ_STATS}))
    dry = DryRunDriver(inner=inner)
    result = dry.pause(_MAILBOX)
    assert result.outcome == ActionOutcome.UNSUPPORTED


def test_name_and_capabilities_pass_through() -> None:
    inner = FakeDriver(name="instantly", capabilities=frozenset({Capability.PAUSE}))
    dry = DryRunDriver(inner=inner)
    assert dry.name == "instantly"
    assert dry.capabilities == frozenset({Capability.PAUSE})


def test_pause_accepts_a_campaign_ref_too() -> None:
    dry = DryRunDriver(inner=FakeDriver())
    result = dry.pause(CampaignRef(provider="fake", campaign_id="camp-1"))
    assert result.outcome == ActionOutcome.PERFORMED
