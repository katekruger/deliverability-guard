"""The provider driver interface. Capability declaration is the whole design.

Not yet implemented — Prompt 2 (BUILD-PLAN.md §5). Two of nine surveyed
providers cannot pause at all (Amplemarket has no status-change API; Salesloft
cadence pause appears UI-only), so the interface must not assume every
provider supports every verb. The engine degrades to alert-only when a verb
is unsupported, and says so in the decision log rather than silently no-oping.

Intended shape:

    class Capability(Enum):
        READ_STATS = auto()
        THROTTLE = auto()
        PAUSE = auto()
        WEBHOOKS = auto()

    class ProviderDriver(Protocol):
        capabilities: frozenset[Capability]

        def read_mailbox_stats(self, since: date) -> list[MailboxDayStats]: ...
        def throttle(self, mailbox_id: str, daily_limit: int) -> ActionResult: ...
        def pause(self, target: MailboxRef | CampaignRef) -> ActionResult: ...
"""
