"""Structural conformance: every CLI-selectable driver satisfies
`ProviderDriver` (CLOSE3-4).

This file exists primarily for pyright, not for the runtime assertion below:
assigning each driver instance to a `ProviderDriver`-typed list makes pyright
reject the file if any driver's method signatures diverge from the Protocol
-- e.g. `LemlistDriver.read_mailbox_stats`'s extra required `campaign_id`
keyword, which is exactly the divergence three audits found and pyright
would have caught here every time. `*CampaignDriver`/`*ConfigurationSetDriver`
adapters (one per campaign- or configuration-set-scoped driver) are what
actually satisfy the Protocol, the pattern `SmartleadCampaignDriver`
established first.

The `assert` gives pytest something to run so `uv run pyright` isn't the
only thing that would catch a regression here -- but the type error IS the
check; a driver missing from this list, or one whose adapter doesn't quite
match, is what this file is for.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

from deliverability_guard.providers.apollo import ApolloCampaignDriver, ApolloDriver
from deliverability_guard.providers.base import ActionOutcome, MailboxRef, ProviderDriver
from deliverability_guard.providers.instantly import InstantlyDriver
from deliverability_guard.providers.lemlist import LemlistCampaignDriver, LemlistDriver
from deliverability_guard.providers.noop import NoopDriver
from deliverability_guard.providers.ses import SesConfigurationSetDriver, SesDriver
from deliverability_guard.providers.smartlead import SmartleadCampaignDriver, SmartleadDriver
from fixtures.http import queued_client, response


class _FakeSesV2Client:
    """Structurally satisfies `providers.ses.SesV2Client` -- same shape as
    `tests/test_ses.py`'s fake, kept minimal since only construction (never
    a call) happens here."""

    def put_configuration_set_sending_options(
        self, *, ConfigurationSetName: str, SendingEnabled: bool
    ) -> Mapping[str, object]:
        return {}

    def put_account_sending_attributes(self, *, SendingEnabled: bool) -> Mapping[str, object]:
        return {}


class _FakeCloudWatchClient:
    """Structurally satisfies `providers.ses.CloudWatchClient`."""

    def get_metric_statistics(
        self,
        *,
        Namespace: str,
        MetricName: str,
        Dimensions: Sequence[Mapping[str, str]],
        StartTime: datetime,
        EndTime: datetime,
        Period: int,
        Statistics: Sequence[str],
    ) -> Mapping[str, object]:
        return {"Datapoints": []}


def test_every_cli_selectable_driver_satisfies_provider_driver() -> None:
    drivers: list[ProviderDriver] = [
        InstantlyDriver(api_key="x"),
        SmartleadCampaignDriver(inner=SmartleadDriver(api_key="x"), campaign_id="c"),
        LemlistCampaignDriver(inner=LemlistDriver(api_key="x"), campaign_id="c"),
        ApolloCampaignDriver(inner=ApolloDriver(api_key="x"), campaign_id="c"),
        SesConfigurationSetDriver(
            inner=SesDriver(
                sesv2_client=_FakeSesV2Client(), cloudwatch_client=_FakeCloudWatchClient()
            ),
            configuration_set_name="cs",
        ),
        NoopDriver(),
    ]
    assert len(drivers) == 6
    assert {d.name for d in drivers} == {
        "instantly",
        "smartlead",
        "lemlist",
        "apollo",
        "ses",
        "noop",
    }


def test_every_driver_declines_an_unsupported_capability_without_raising() -> None:
    """CLOSE5-3: README's own claim -- 'every driver's `pause()`/`throttle()`
    is always callable and returns an explicit "unsupported" result rather
    than silently doing nothing when a provider lacks a capability' -- had
    no executable guard behind it.

    Deliberately exercises ONLY the (driver, verb) pairs each driver
    declines structurally -- e.g. Smartlead's per-mailbox `pause`, which
    every driver's own `isinstance(target, MailboxRef)` check answers
    without ever reaching the network -- never a pair the driver actually
    implements. Instantly/Smartlead/Lemlist/Apollo are constructed with a
    real `httpx.Client` and no injected fake, so calling a capability they
    DO support here would be a live call (AGENTS.md); this test's whole
    point is that it doesn't need to, because `unsupported()` returns
    before any request is built."""
    mailbox = MailboxRef(provider="x", mailbox_id="a@example.com")
    cases: list[tuple[ProviderDriver, str]] = [
        (InstantlyDriver(api_key="x"), "throttle"),  # no daily-limit endpoint at all
        (
            SmartleadCampaignDriver(inner=SmartleadDriver(api_key="x"), campaign_id="c"),
            "pause",
        ),  # no per-mailbox pause endpoint
        (LemlistCampaignDriver(inner=LemlistDriver(api_key="x"), campaign_id="c"), "pause"),
        (LemlistCampaignDriver(inner=LemlistDriver(api_key="x"), campaign_id="c"), "throttle"),
        (ApolloCampaignDriver(inner=ApolloDriver(api_key="x"), campaign_id="c"), "pause"),
        (ApolloCampaignDriver(inner=ApolloDriver(api_key="x"), campaign_id="c"), "throttle"),
        (
            SesConfigurationSetDriver(
                inner=SesDriver(
                    sesv2_client=_FakeSesV2Client(), cloudwatch_client=_FakeCloudWatchClient()
                ),
                configuration_set_name="cs",
            ),
            "pause",
        ),
        (
            SesConfigurationSetDriver(
                inner=SesDriver(
                    sesv2_client=_FakeSesV2Client(), cloudwatch_client=_FakeCloudWatchClient()
                ),
                configuration_set_name="cs",
            ),
            "throttle",
        ),
        (NoopDriver(), "pause"),
        (NoopDriver(), "throttle"),
    ]
    for driver, verb in cases:
        result = driver.pause(mailbox) if verb == "pause" else driver.throttle("a@example.com", 100)
        assert result.outcome is ActionOutcome.UNSUPPORTED, (driver.name, verb)


def test_pause_and_throttle_via_mailboxref_reach_exactly_one_provider_each() -> None:
    """CLOSE6-3: README.md's capability-matrix paragraph claims 'PAUSE
    actually executes against exactly one provider (`instantly`), and
    THROTTLE against exactly one (`smartlead`); no single provider can do
    both' -- the `CampaignRef` half of that same sentence gained an
    executable guard in CLOSE5-3
    (`test_engine_breaker_module_never_constructs_a_campaign_ref`), but
    this half never did.

    `engine.breaker.evaluate`'s ladder only ever constructs a `MailboxRef`
    (that CLOSE5-3 guard is what pins that down), so what matters here is
    each driver's response to `pause(MailboxRef)`/`throttle(...)`
    specifically -- not `driver.capabilities`, which several of these
    providers declare for their CAMPAIGN-level primitives even though the
    breaker can never reach them. Instantly's `pause` and Smartlead's
    `throttle` are exercised against recorded fixtures (never live,
    AGENTS.md); every other (driver, verb) pair here is already proven
    UNSUPPORTED by `test_every_driver_declines_an_unsupported_capability_
    without_raising` above -- this test's job is only to confirm the
    POSITIVE half: that exactly one driver PERFORMS each verb."""
    mailbox = MailboxRef(provider="x", mailbox_id="a@example.com")

    instantly = InstantlyDriver(
        api_key="x",
        client=queued_client(
            [response(200, "instantly/pause_account_200.json")],
            base_url="https://api.instantly.ai",
        ),
    )
    smartlead = SmartleadCampaignDriver(
        inner=SmartleadDriver(
            api_key="x",
            client=queued_client(
                [response(200, "smartlead/email_account_update_200.json")],
                base_url="https://server.smartlead.ai/api/v1",
            ),
            sleep=lambda _seconds: None,
        ),
        campaign_id="c",
    )
    others: list[ProviderDriver] = [
        SmartleadCampaignDriver(inner=SmartleadDriver(api_key="x"), campaign_id="c"),
        LemlistCampaignDriver(inner=LemlistDriver(api_key="x"), campaign_id="c"),
        ApolloCampaignDriver(inner=ApolloDriver(api_key="x"), campaign_id="c"),
        SesConfigurationSetDriver(
            inner=SesDriver(
                sesv2_client=_FakeSesV2Client(), cloudwatch_client=_FakeCloudWatchClient()
            ),
            configuration_set_name="cs",
        ),
        NoopDriver(),
    ]

    pausers = [instantly, *others]
    pause_outcomes = {driver.name: driver.pause(mailbox).outcome for driver in pausers}
    assert pause_outcomes["instantly"] is ActionOutcome.PERFORMED
    performed_pausers = [
        name for name, outcome in pause_outcomes.items() if outcome is ActionOutcome.PERFORMED
    ]
    assert performed_pausers == ["instantly"]

    throttlers: list[ProviderDriver] = [
        InstantlyDriver(api_key="x"),
        smartlead,
        LemlistCampaignDriver(inner=LemlistDriver(api_key="x"), campaign_id="c"),
        ApolloCampaignDriver(inner=ApolloDriver(api_key="x"), campaign_id="c"),
        SesConfigurationSetDriver(
            inner=SesDriver(
                sesv2_client=_FakeSesV2Client(), cloudwatch_client=_FakeCloudWatchClient()
            ),
            configuration_set_name="cs",
        ),
        NoopDriver(),
    ]
    throttle_outcomes = {
        driver.name: driver.throttle("a@example.com", 100).outcome for driver in throttlers
    }
    assert throttle_outcomes["smartlead"] is ActionOutcome.PERFORMED
    performed_throttlers = [
        name for name, outcome in throttle_outcomes.items() if outcome is ActionOutcome.PERFORMED
    ]
    assert performed_throttlers == ["smartlead"]
