"""A credential-free driver that reports a small synthetic fixture and
can't act on any of it.

Exists purely so `deliverability-guard check`/`run` can be exercised end to
end -- config loading, aggregation, evaluation (including hierarchical
pooling, since the fixture is two mailboxes sharing a domain), decision-log
writing, exit codes -- without a live provider account (CLOSE-5: before
this, `cli.cmd_check`'s own wiring could only be tested by calling it
directly with a Python `FakeDriver`, never through `cli.main` and
`build_driver` the way a real user actually invokes it). Select it with
`provider: noop` in `config/thresholds.yml`; no environment variable is
read for it, unlike every other driver `cli.build_driver` constructs.

CLOSE3-4: this used to report NO mailboxes at all, which meant `check`
exited via the early "no mailboxes reported any stats" branch -- exercising
config loading and the exit path, but never the aggregation, evaluation, or
decision-log-writing README line 62 claimed it did. No log file was even
created. The synthetic fixture below is intentionally boring (healthy,
zero-bounce) -- this is a smoke driver proving the pipeline runs, not a
scenario generator; use `FakeDriver` (below) for anything that needs
specific verdicts or forced outcomes.

This is NOT a test double -- `tests/fixtures/fake_driver.py`'s `FakeDriver`
is still what tests use, since it's configurable (forced outcomes, raised
exceptions, canned stats) in ways this driver deliberately is not. This is
a real, shipped `ProviderDriver` implementation that reports one fixed,
healthy fixture and supports no action on it.
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
_DOMAIN = "noop.example"


class NoopDriver:
    """See module docstring."""

    name = _PROVIDER
    capabilities = frozenset({Capability.READ_STATS})

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        # `day=since` rather than a hardcoded date -- always "today's"
        # aggregation window for whatever `now` the caller actually used,
        # the same way a real provider's most recent day would be. Two
        # mailboxes sharing `_DOMAIN` so hierarchical pooling
        # (`engine.posterior.pooled_posterior`) is exercised too, not just a
        # flat evaluation.
        return [
            MailboxDayStats(
                mailbox=MailboxRef(provider=_PROVIDER, mailbox_id=f"demo-1@{_DOMAIN}"),
                day=since,
                sends=200,
                bounces=0,
            ),
            MailboxDayStats(
                mailbox=MailboxRef(provider=_PROVIDER, mailbox_id=f"demo-2@{_DOMAIN}"),
                day=since,
                sends=150,
                bounces=0,
            ),
        ]

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return unsupported(Capability.THROTTLE, self.name, "the noop driver supports nothing")

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        return unsupported(Capability.PAUSE, self.name, "the noop driver supports nothing")
