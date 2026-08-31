"""Tests for providers/ses.py -- against fake boto3-shaped clients only,
never a live AWS account (AGENTS.md: no live API calls in tests)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

import pytest

from deliverability_guard.providers.base import (
    ActionOutcome,
    CampaignRef,
    Capability,
    MailboxRef,
    MalformedResponseError,
)
from deliverability_guard.providers.ses import SesConfigurationSetDriver, SesDriver

_NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


class _FakeSesV2Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.raise_on_call: Exception | None = None

    def put_configuration_set_sending_options(
        self, *, ConfigurationSetName: str, SendingEnabled: bool
    ) -> Mapping[str, object]:
        self.calls.append(
            (
                "put_configuration_set_sending_options",
                {"ConfigurationSetName": ConfigurationSetName, "SendingEnabled": SendingEnabled},
            )
        )
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return {}

    def put_account_sending_attributes(self, *, SendingEnabled: bool) -> Mapping[str, object]:
        self.calls.append(("put_account_sending_attributes", {"SendingEnabled": SendingEnabled}))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return {}


class _FakeCloudWatchClient:
    def __init__(self, responses: dict[str, list[dict[str, object]]]) -> None:
        # keyed by MetricName -> list of raw Datapoints dicts
        self._responses = responses
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "Namespace": Namespace,
                "MetricName": MetricName,
                "Dimensions": list(Dimensions),
                "StartTime": StartTime,
                "EndTime": EndTime,
                "Period": Period,
                "Statistics": list(Statistics),
            }
        )
        return {"Datapoints": self._responses.get(MetricName, []), "Label": MetricName}


def _driver(sesv2: _FakeSesV2Client, cloudwatch: _FakeCloudWatchClient) -> SesDriver:
    return SesDriver(sesv2_client=sesv2, cloudwatch_client=cloudwatch)


def test_read_mailbox_stats_combines_send_and_bounce_metrics_per_day() -> None:
    cloudwatch = _FakeCloudWatchClient(
        {
            "Send": [{"Timestamp": datetime(2026, 8, 1, tzinfo=UTC), "Sum": 5000.0}],
            "Bounce": [{"Timestamp": datetime(2026, 8, 1, tzinfo=UTC), "Sum": 40.0}],
        }
    )
    driver = _driver(_FakeSesV2Client(), cloudwatch)

    stats = driver.read_mailbox_stats(
        since=date(2026, 8, 1), configuration_set_name="cfg-1", now=_NOW
    )

    assert len(stats) == 1
    assert stats[0].mailbox == MailboxRef(provider="ses", mailbox_id="cfg-1")
    assert stats[0].day == date(2026, 8, 1)
    assert stats[0].sends == 5000
    assert stats[0].bounces == 40


def test_read_mailbox_stats_handles_a_day_with_sends_but_no_bounces() -> None:
    cloudwatch = _FakeCloudWatchClient(
        {"Send": [{"Timestamp": datetime(2026, 8, 1, tzinfo=UTC), "Sum": 100.0}], "Bounce": []}
    )
    driver = _driver(_FakeSesV2Client(), cloudwatch)

    stats = driver.read_mailbox_stats(
        since=date(2026, 8, 1), configuration_set_name="cfg-1", now=_NOW
    )

    assert len(stats) == 1
    assert stats[0].sends == 100
    assert stats[0].bounces == 0


def test_read_mailbox_stats_queries_the_right_namespace_and_dimension() -> None:
    cloudwatch = _FakeCloudWatchClient({"Send": [], "Bounce": []})
    driver = _driver(_FakeSesV2Client(), cloudwatch)

    driver.read_mailbox_stats(since=date(2026, 8, 1), configuration_set_name="cfg-1", now=_NOW)

    send_call = cloudwatch.calls[0]
    assert send_call["Namespace"] == "AWS/SES"
    assert send_call["MetricName"] == "Send"
    assert send_call["Dimensions"] == [{"Name": "ses:configuration-set", "Value": "cfg-1"}]
    assert send_call["EndTime"] == _NOW


def test_read_mailbox_stats_with_no_datapoints_is_an_empty_list() -> None:
    cloudwatch = _FakeCloudWatchClient({"Send": [], "Bounce": []})
    driver = _driver(_FakeSesV2Client(), cloudwatch)
    stats = driver.read_mailbox_stats(
        since=date(2026, 8, 1), configuration_set_name="cfg-1", now=_NOW
    )
    assert stats == []


def test_read_mailbox_stats_raises_on_missing_datapoints_key() -> None:
    class _BrokenCloudWatch:
        def get_metric_statistics(self, **kwargs: object) -> Mapping[str, object]:
            return {"NotDatapoints": []}

    driver = _driver(_FakeSesV2Client(), _BrokenCloudWatch())  # type: ignore[arg-type]
    with pytest.raises(MalformedResponseError, match="Datapoints"):
        driver.read_mailbox_stats(since=date(2026, 8, 1), configuration_set_name="cfg-1", now=_NOW)


def test_read_mailbox_stats_raises_on_a_malformed_datapoint() -> None:
    cloudwatch = _FakeCloudWatchClient(
        {"Send": [{"Timestamp": "not-a-datetime", "Sum": 1}], "Bounce": []}
    )
    driver = _driver(_FakeSesV2Client(), cloudwatch)
    with pytest.raises(MalformedResponseError):
        driver.read_mailbox_stats(since=date(2026, 8, 1), configuration_set_name="cfg-1", now=_NOW)


def test_read_mailbox_stats_raises_when_a_datapoint_is_not_a_mapping() -> None:
    cloudwatch = _FakeCloudWatchClient({"Send": ["not-a-dict"], "Bounce": []})  # type: ignore[dict-item]
    driver = _driver(_FakeSesV2Client(), cloudwatch)
    with pytest.raises(MalformedResponseError, match="datapoint"):
        driver.read_mailbox_stats(since=date(2026, 8, 1), configuration_set_name="cfg-1", now=_NOW)


def test_throttle_is_unsupported() -> None:
    driver = _driver(_FakeSesV2Client(), _FakeCloudWatchClient({}))
    result = driver.throttle("cfg-1", 100)
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.THROTTLE


def test_pause_mailbox_is_unsupported() -> None:
    driver = _driver(_FakeSesV2Client(), _FakeCloudWatchClient({}))
    result = driver.pause(MailboxRef(provider="ses", mailbox_id="whatever"))
    assert result.outcome == ActionOutcome.UNSUPPORTED
    assert result.capability == Capability.PAUSE


def test_pause_campaign_disables_the_configuration_set() -> None:
    sesv2 = _FakeSesV2Client()
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    result = driver.pause(CampaignRef(provider="ses", campaign_id="cfg-1"))
    assert result.outcome == ActionOutcome.PERFORMED
    assert sesv2.calls == [
        (
            "put_configuration_set_sending_options",
            {"ConfigurationSetName": "cfg-1", "SendingEnabled": False},
        )
    ]


def test_pause_configuration_set_failure_is_reported_not_raised() -> None:
    sesv2 = _FakeSesV2Client()
    sesv2.raise_on_call = RuntimeError("AWS ClientError: AccessDenied")
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    result = driver.pause_configuration_set("cfg-1")
    assert result.outcome == ActionOutcome.FAILED
    assert "AccessDenied" in result.detail


def test_resume_configuration_set_enables_sending() -> None:
    sesv2 = _FakeSesV2Client()
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    result = driver.resume_configuration_set("cfg-1")
    assert result.outcome == ActionOutcome.PERFORMED
    assert sesv2.calls == [
        (
            "put_configuration_set_sending_options",
            {"ConfigurationSetName": "cfg-1", "SendingEnabled": True},
        )
    ]


def test_pause_account_disables_account_wide_sending() -> None:
    sesv2 = _FakeSesV2Client()
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    result = driver.pause_account()
    assert result.outcome == ActionOutcome.PERFORMED
    assert sesv2.calls == [("put_account_sending_attributes", {"SendingEnabled": False})]


def test_pause_account_failure_is_reported_not_raised() -> None:
    sesv2 = _FakeSesV2Client()
    sesv2.raise_on_call = RuntimeError("AWS ClientError: Throttling")
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    result = driver.pause_account()
    assert result.outcome == ActionOutcome.FAILED


def test_resume_account_enables_account_wide_sending() -> None:
    sesv2 = _FakeSesV2Client()
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    result = driver.resume_account()
    assert result.outcome == ActionOutcome.PERFORMED
    assert sesv2.calls == [("put_account_sending_attributes", {"SendingEnabled": True})]


def test_resume_account_failure_is_reported_not_raised() -> None:
    sesv2 = _FakeSesV2Client()
    sesv2.raise_on_call = RuntimeError("AWS ClientError: Throttling")
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    result = driver.resume_account()
    assert result.outcome == ActionOutcome.FAILED


def test_pause_account_is_not_reachable_through_the_generic_pause_verb() -> None:
    """The account-wide kill switch must only be reachable via its own
    named method, never implicitly through pause(MailboxRef) or
    pause(CampaignRef) -- a disproportionate blast radius must be
    deliberate."""
    sesv2 = _FakeSesV2Client()
    driver = _driver(sesv2, _FakeCloudWatchClient({}))
    driver.pause(MailboxRef(provider="ses", mailbox_id="whatever"))
    driver.pause(CampaignRef(provider="ses", campaign_id="cfg-1"))
    assert all(call[0] != "put_account_sending_attributes" for call in sesv2.calls)


def test_capabilities_declare_no_throttle_or_webhooks() -> None:
    assert Capability.THROTTLE not in SesDriver.capabilities
    assert Capability.WEBHOOKS not in SesDriver.capabilities
    assert Capability.PAUSE in SesDriver.capabilities
    assert Capability.READ_STATS in SesDriver.capabilities


# --- SesConfigurationSetDriver: the ProviderDriver adapter (CLOSE3-4) ------


def test_configuration_set_driver_pins_read_mailbox_stats() -> None:
    sesv2 = _FakeSesV2Client()
    cloudwatch = _FakeCloudWatchClient({"Send": [{"Timestamp": _NOW, "Sum": 100.0}]})
    driver = SesConfigurationSetDriver(
        inner=_driver(sesv2, cloudwatch), configuration_set_name="cs-1"
    )
    stats = driver.read_mailbox_stats(date(2026, 8, 2))
    assert len(stats) > 0
    assert stats[0].mailbox.mailbox_id == "cs-1"


def test_configuration_set_driver_passes_through_name_and_capabilities() -> None:
    inner = _driver(_FakeSesV2Client(), _FakeCloudWatchClient({}))
    driver = SesConfigurationSetDriver(inner=inner, configuration_set_name="cs-1")
    assert driver.name == inner.name
    assert driver.capabilities == inner.capabilities


def test_configuration_set_driver_throttle_passes_through() -> None:
    inner = _driver(_FakeSesV2Client(), _FakeCloudWatchClient({}))
    driver = SesConfigurationSetDriver(inner=inner, configuration_set_name="cs-1")
    result = driver.throttle("acct-1", 25)
    assert result.outcome == ActionOutcome.UNSUPPORTED


def test_configuration_set_driver_pause_passes_through() -> None:
    sesv2 = _FakeSesV2Client()
    driver = SesConfigurationSetDriver(
        inner=_driver(sesv2, _FakeCloudWatchClient({})), configuration_set_name="cs-1"
    )
    result = driver.pause(CampaignRef(provider="ses", campaign_id="cs-1"))
    assert result.outcome == ActionOutcome.PERFORMED
