"""Amazon SES provider driver.

    Read stats: Amazon CloudWatch, namespace `AWS/SES`, metrics `Send` and
               `Bounce`, dimensioned by `ses:configuration-set`
    Pause:     SESv2 `PutConfigurationSetSendingOptions` (a configuration
               set) or `PutAccountSendingAttributes` (the whole account)
    Bounce feed: Amazon SNS topic subscriptions -- NOT implemented here;
               see the module-level "Known limitation" below

BUILD-PLAN.md §5's capability matrix lists SES's per-mailbox pause/throttle
column as "account": unlike Instantly or Smartlead, SES has no concept of
an individual "mailbox" at all -- sending happens through verified
identities against an account-wide (or configuration-set-scoped) sending
quota and rate. The two actionable units are a configuration set and the
whole account, which is why `capabilities` here is READ_STATS + PAUSE with
no THROTTLE (SES's rate control is a max-sends-per-second value, not a
daily-volume limit the way Smartlead's throttle is, and mapping one onto
the other would misrepresent what actually happens) and no WEBHOOKS (SES
delivers bounce/complaint notifications via SNS, a push mechanism this
driver does not implement -- see "Known limitation").

BUILD-PLAN.md §13 flagged the exact action names as an open question
("Likely `UpdateAccountSendingEnabled` / `PutAccountSendingAttributes` but
unverified"). This driver uses the SESv2 API's own naming
(`put_account_sending_attributes`, `put_configuration_set_sending_options`
in boto3's snake_case convention) -- verify against a real AWS account
before trusting it against production traffic, same caveat every other
driver in this project carries for its own endpoint shapes.

Known limitation: this driver has no SNS bounce/complaint ingestion.
BUILD-PLAN.md's fast loop expects near-real-time bounce signals; for SES,
building that means subscribing an SNS topic (or an SQS queue behind one)
and parsing ARF-shaped notification payloads -- real, separately-scoped
infrastructure work, not a small addition here. `read_mailbox_stats` below
is a polling substitute (CloudWatch's daily `Send`/`Bounce` sums), with the
same latency tradeoff ADR 0004 already accepts for the two-loop daemon's
fast loop generally.

`boto3` is a new runtime dependency for this driver alone -- see the PR
this shipped in for the one-line justification AGENTS.md requires, and ADR
0005 for why this is worth the added weight rather than hand-rolling AWS's
SigV4 request signing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol, cast

from deliverability_guard.providers._parsing import require_dict, require_list
from deliverability_guard.providers.base import (
    ActionOutcome,
    ActionResult,
    CampaignRef,
    Capability,
    MailboxDayStats,
    MailboxRef,
    MalformedResponseError,
    unsupported,
)

_PROVIDER = "ses"
_NAMESPACE = "AWS/SES"
_CONFIG_SET_DIMENSION = "ses:configuration-set"


class SesV2Client(Protocol):
    """The slice of boto3's `sesv2` client this driver actually calls.
    Defined here rather than depending on `boto3-stubs`/`mypy-boto3-sesv2`
    -- boto3's own runtime client has no static types at all, so this
    Protocol is what makes the driver itself type-checkable under pyright
    strict without adding yet another dependency just for stubs.
    """

    def put_configuration_set_sending_options(
        self, *, ConfigurationSetName: str, SendingEnabled: bool
    ) -> Mapping[str, object]: ...

    def put_account_sending_attributes(self, *, SendingEnabled: bool) -> Mapping[str, object]: ...


class CloudWatchClient(Protocol):
    """The slice of boto3's `cloudwatch` client this driver calls. See
    `SesV2Client` for why this is a hand-defined Protocol, not a stub
    import."""

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
    ) -> Mapping[str, object]: ...


def _new_sesv2_client(region_name: str | None) -> SesV2Client:  # pragma: no cover -- real AWS creds
    import boto3

    return cast(
        SesV2Client,
        boto3.client("sesv2", region_name=region_name),  # pyright: ignore[reportUnknownMemberType]
    )


def _new_cloudwatch_client(region_name: str | None) -> CloudWatchClient:  # pragma: no cover
    import boto3

    return cast(
        CloudWatchClient,
        boto3.client("cloudwatch", region_name=region_name),  # pyright: ignore[reportUnknownMemberType]
    )


class SesDriver:
    """See module docstring. `configuration_set_name` scopes both
    `read_mailbox_stats` (which CloudWatch dimension to read) and `pause`
    for a `CampaignRef` (which configuration set to disable) -- SES has no
    global per-mailbox feed any more than Smartlead or Apollo do.
    """

    name = _PROVIDER
    capabilities = frozenset({Capability.READ_STATS, Capability.PAUSE})

    def __init__(
        self,
        *,
        sesv2_client: SesV2Client | None = None,
        cloudwatch_client: CloudWatchClient | None = None,
        region_name: str | None = None,
    ) -> None:
        """`region_name` is passed straight through to `boto3.client(...)`
        when this driver constructs its own clients (i.e. when
        `sesv2_client`/`cloudwatch_client` aren't given) -- explicit, rather
        than relying on ambient `AWS_DEFAULT_REGION` process environment,
        so `cli.build_driver` can construct a real driver deterministically
        from the one config source it already reads everything else from
        (CLOSE3-4). Ignored when either client is given directly."""
        self._sesv2 = sesv2_client if sesv2_client is not None else _new_sesv2_client(region_name)
        self._cloudwatch = (
            cloudwatch_client
            if cloudwatch_client is not None
            else _new_cloudwatch_client(region_name)
        )

    def read_mailbox_stats(
        self,
        since: date,
        *,
        configuration_set_name: str,
        now: datetime | None = None,
    ) -> list[MailboxDayStats]:
        """SES has no mailboxes, only configuration sets -- this driver
        represents the configuration set itself as a single synthetic
        `MailboxRef` so it can still flow through `engine.breaker.evaluate`
        unmodified. Daily send/bounce counts come from CloudWatch's
        `Send`/`Bounce` metric sums, one datapoint per UTC day.

        `now` bounds the CloudWatch query window and is injectable for
        deterministic tests; it defaults to the real clock, unlike this
        project's engine-layer functions (`engine.breaker.evaluate`, etc.)
        where `now` has no default at all -- this is a read-only query
        window, not a decision input, so a default is safe here in a way
        it deliberately isn't for anything that can act on a mailbox.
        """
        until = now if now is not None else datetime.now(UTC)
        dimensions = [{"Name": _CONFIG_SET_DIMENSION, "Value": configuration_set_name}]
        sends_by_day = self._daily_sums("Send", dimensions, since, until)
        bounces_by_day = self._daily_sums("Bounce", dimensions, since, until)

        mailbox = MailboxRef(provider=_PROVIDER, mailbox_id=configuration_set_name)
        days = sorted(set(sends_by_day) | set(bounces_by_day))
        return [
            MailboxDayStats(
                mailbox=mailbox,
                day=day,
                sends=sends_by_day.get(day, 0),
                bounces=bounces_by_day.get(day, 0),
            )
            for day in days
        ]

    def _daily_sums(
        self,
        metric_name: str,
        dimensions: list[dict[str, str]],
        since: date,
        until: datetime,
    ) -> dict[date, int]:
        response = self._cloudwatch.get_metric_statistics(
            Namespace=_NAMESPACE,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=datetime.combine(since, datetime.min.time()),
            EndTime=until,
            Period=86400,
            Statistics=["Sum"],
        )
        datapoints = require_list(response.get("Datapoints"), _PROVIDER, "'Datapoints'")
        totals: dict[date, int] = {}
        for raw_point in datapoints:
            point = require_dict(raw_point, _PROVIDER, f"{metric_name} datapoint")
            timestamp = point.get("Timestamp")
            total = point.get("Sum")
            if not isinstance(timestamp, datetime) or not isinstance(total, int | float):
                raise MalformedResponseError(
                    f"{_PROVIDER}: {metric_name} datapoint missing 'Timestamp'/'Sum'"
                )
            totals[timestamp.date()] = int(total)
        return totals

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return unsupported(
            Capability.THROTTLE,
            self.name,
            "SES's rate control is a max-sends-per-second value, not a daily-volume "
            "limit -- there is no primitive this maps onto",
        )

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        if isinstance(target, MailboxRef):
            return unsupported(
                Capability.PAUSE,
                self.name,
                "SES has no per-mailbox pause primitive narrower than a configuration "
                "set; pause a CampaignRef (treated as a configuration set name), or "
                "call pause_account() for the account-wide kill switch",
            )
        return self.pause_configuration_set(target.campaign_id)

    def pause_configuration_set(self, configuration_set_name: str) -> ActionResult:
        return self._set_configuration_set_sending(configuration_set_name, enabled=False)

    def resume_configuration_set(self, configuration_set_name: str) -> ActionResult:
        """SES-specific, symmetric with `pause_configuration_set`. Not a
        generic un-pause verb on the base Protocol -- AGENTS.md: never
        auto-resume without a human; this exists for an operator to call
        deliberately."""
        return self._set_configuration_set_sending(configuration_set_name, enabled=True)

    def _set_configuration_set_sending(
        self, configuration_set_name: str, *, enabled: bool
    ) -> ActionResult:
        try:
            self._sesv2.put_configuration_set_sending_options(
                ConfigurationSetName=configuration_set_name, SendingEnabled=enabled
            )
        except Exception as exc:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: configuration set sending update failed: {exc}",
                capability=Capability.PAUSE,
            )
        verb = "disabled" if not enabled else "enabled"
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: configuration set {configuration_set_name} sending {verb}",
            capability=Capability.PAUSE,
        )

    def pause_account(self) -> ActionResult:
        """The account-wide kill switch -- disables sending for EVERY
        configuration set and identity in the AWS account, not just one
        campaign or mailbox. Deliberately unreachable through the generic
        `pause()` Protocol method (which only ever affects one
        `MailboxRef`/`CampaignRef`), for the same reason
        `providers/smartlead.py` refuses to let a per-mailbox pause request
        silently escalate to a whole-campaign action: a disproportionate
        blast radius must be something an operator reaches for
        deliberately, never something a generic call reaches by accident.
        """
        try:
            self._sesv2.put_account_sending_attributes(SendingEnabled=False)
        except Exception as exc:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: account sending update failed: {exc}",
                capability=Capability.PAUSE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: account-wide sending disabled",
            capability=Capability.PAUSE,
        )

    def resume_account(self) -> ActionResult:
        """Symmetric with `pause_account`; same never-automatic-resume
        rationale as every other driver's resume method in this project."""
        try:
            self._sesv2.put_account_sending_attributes(SendingEnabled=True)
        except Exception as exc:
            return ActionResult(
                outcome=ActionOutcome.FAILED,
                detail=f"{_PROVIDER}: account sending update failed: {exc}",
                capability=Capability.PAUSE,
            )
        return ActionResult(
            outcome=ActionOutcome.PERFORMED,
            detail=f"{_PROVIDER}: account-wide sending enabled",
            capability=Capability.PAUSE,
        )


@dataclass(frozen=True, slots=True)
class SesConfigurationSetDriver:
    """Adapts `SesDriver` to the generic `ProviderDriver` Protocol by pinning
    `read_mailbox_stats` to one configuration set (CLOSE3-4) -- same pattern
    `providers.smartlead.SmartleadCampaignDriver` established for Smartlead's
    equally per-campaign statistics endpoint. Every other method passes
    straight through to `inner`."""

    inner: SesDriver
    configuration_set_name: str

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.inner.capabilities

    def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]:
        return self.inner.read_mailbox_stats(
            since, configuration_set_name=self.configuration_set_name
        )

    def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult:
        return self.inner.throttle(mailbox_id, daily_limit)

    def pause(self, target: MailboxRef | CampaignRef) -> ActionResult:
        return self.inner.pause(target)
