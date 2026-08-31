"""A credential-free driver that reports no mailboxes and can't act on any.

Exists purely so `deliverability-guard check`/`run` can be exercised end to
end -- config loading, the aggregation-and-evaluation path, decision-log
writing, exit codes -- without a live provider account (CLOSE-5: before
this, `cli.cmd_check`'s own wiring could only be tested by calling it
directly with a Python `FakeDriver`, never through `cli.main` and
`build_driver` the way a real user actually invokes it). Select it with
`provider: noop` in `config/thresholds.yml`; no environment variable is
read for it, unlike every other driver `cli.build_driver` constructs.

This is NOT a test double -- `tests/fixtures/fake_driver.py`'s `FakeDriver`
is still what tests use, since it's configurable (forced outcomes, raised
exceptions, canned stats) in ways this driver deliberately is not. This is
a real, shipped `ProviderDriver` implementation with exactly one behavior:
report nothing, support nothing.
"""

from datetime import date

from deliverability_guard.providers.base import (
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
    unsupported,
)

_PROVIDER = "noop"


class NoopDriver:
    """See module docstring."""

    name = _PROVIDER
    capabilities = frozenset({Capability.READ_STATS})

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        return []

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return unsupported(Capability.THROTTLE, self.name, "the noop driver supports nothing")

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        return unsupported(Capability.PAUSE, self.name, "the noop driver supports nothing")
