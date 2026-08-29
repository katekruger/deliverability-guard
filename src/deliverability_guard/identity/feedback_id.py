"""The Feedback-ID scheme: campaign:segment:mailbox:tenant.

Postmaster v2 exposes `FEEDBACK_LOOP_ID` and `FEEDBACK_LOOP_SPAM_RATE`
(BUILD-PLAN.md §5, §9). Setting a `Feedback-ID:` header per campaign makes
Postmaster report spam rate PER CAMPAIGN -- the best attribution mechanism
available in email, and badly underused. This module generates and
validates that header's value; it does not send mail or modify outgoing
messages -- see `docs/limits.md` for why the attribution problem can't be
solved after the fact, only at send time.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

_SEPARATOR = ":"
_COMPONENT_NAMES = ("campaign", "segment", "mailbox", "tenant")
# Conservative: letters, digits, hyphen, underscore, dot. Feedback-ID rides
# in an RFC 5322 header value; rather than guess at exactly what Gmail's
# parser tolerates, this stays inside a safe subset no mail header parser
# would choke on.
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class FeedbackId:
    campaign: str
    segment: str
    mailbox: str
    tenant: str

    def __post_init__(self) -> None:
        for name, value in zip(
            _COMPONENT_NAMES, (self.campaign, self.segment, self.mailbox, self.tenant), strict=True
        ):
            _validate_component(name, value)

    def encode(self) -> str:
        return _SEPARATOR.join((self.campaign, self.segment, self.mailbox, self.tenant))


def _validate_component(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must not be empty")
    if not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(
            f"{name} contains characters outside the safe set "
            f"(letters, digits, '.', '_', '-'): {value!r}"
        )


def parse(raw: str) -> FeedbackId:
    """Parse a `Feedback-ID:` header value back into its components.

    Raises `ValueError` on anything that doesn't cleanly parse -- callers
    doing coverage checking (see `check_coverage` below) are expected to
    catch this per header, not let one malformed value abort a whole batch.
    """
    parts = raw.split(_SEPARATOR)
    if len(parts) != len(_COMPONENT_NAMES):
        raise ValueError(
            f"expected {len(_COMPONENT_NAMES)} '{_SEPARATOR}'-separated components "
            f"({':'.join(_COMPONENT_NAMES)}), got {len(parts)}: {raw!r}"
        )
    return FeedbackId(*parts)


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """How much of a batch of outgoing mail actually carried a valid
    Feedback-ID -- BUILD-PLAN.md §8: "Feedback-ID present on some mail,
    absent on others -> report partial coverage with the percentage, don't
    silently under-attribute." `percentage()` returns `None` for an empty
    batch rather than a misleading 0.0 -- "no messages checked" is not the
    same claim as "0% had a Feedback-ID."
    """

    total: int
    with_feedback_id: int
    invalid: tuple[str, ...]

    def percentage(self) -> float | None:
        if self.total == 0:
            return None
        return self.with_feedback_id / self.total


def check_coverage(headers: Iterable[str | None]) -> CoverageReport:
    """`headers` is one entry per outgoing message: its `Feedback-ID` header
    value, or `None` if the header was absent entirely. A present-but-
    unparseable value counts as neither covered nor absent -- it's reported
    separately in `invalid`, since "someone set a header, but it's wrong" is
    a different problem (and a different fix) than "no one set a header."
    """
    total = 0
    with_id = 0
    invalid: list[str] = []
    for header in headers:
        total += 1
        if header is None:
            continue
        try:
            parse(header)
        except ValueError:
            invalid.append(header)
            continue
        with_id += 1
    return CoverageReport(total=total, with_feedback_id=with_id, invalid=tuple(invalid))
