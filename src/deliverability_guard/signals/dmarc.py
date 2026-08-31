"""DMARC aggregate-report auth-health signal, built on `parsedmarc`
(BUILD-PLAN.md §4 item #17: "Reuse as a library. Do not reimplement" --
`parsedmarc` already parses DMARC aggregate XML, handles gzip/zip
attachments, and can pull reports straight from an IMAP mailbox).

This module never parses raw DMARC XML itself. `summarize_auth_health`
takes `parsedmarc`'s own parsed-report dicts -- e.g. the return value of
`parsedmarc.parse_aggregate_report_xml()` or one entry from
`parsedmarc.get_dmarc_reports_from_mailbox()["aggregate_reports"]` -- and
aggregates them into one summary. This project owns the aggregation
policy (alignment classification, unknown-source ranking, what counts as
"no data"); `parsedmarc` owns parsing the wire format.

**Auth health is a slow-loop, cross-provider signal, not a fast-loop one**
(BUILD-PLAN.md §5): DMARC aggregate reports (RUA) arrive from receiving
mail servers roughly daily, in arrears, the same delayed-evidence shape as
the complaint data `engine/posterior.py`'s entire thesis is built around --
this is not a real-time bounce/complaint signal and must never be
mistaken for one. It also carries no complaint or inbox-placement data at
all (BUILD-PLAN.md §8): a message can be perfectly DMARC-aligned and still
be complained about, or misaligned and still land in the inbox. Treat this
purely as "is traffic claiming to be my domain authenticating," nothing
more.

Cite RFC 9989 (DMARC), which obsoletes RFC 7489, and RFC 9990/9991
(aggregate/failure reporting) -- BUILD-PLAN.md §8 flags this because a lot
of published DMARC guidance still cites 7489.

DMARC alignment (RFC 9989 §3): a message passes DMARC if EITHER SPF or
DKIM is aligned with the `header_from` domain, not both. A record aligned
on neither is genuinely unauthenticated traffic riding on the domain's
reputation -- the "unknown-source detection" BUILD-PLAN.md asks for,
surfaced here as a ranked list of sources so a human can see who's
actually sending unauthenticated mail as their domain, not just a bare
rate.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

from deliverability_guard.engine.state import DataState


class MalformedDmarcReportError(Exception):
    """A report dict passed to `summarize_auth_health` doesn't match
    parsedmarc's documented output shape -- e.g. a missing `records` key,
    or a record missing `alignment`/`count`. Raised rather than silently
    skipping the record or coercing a missing value to a default: an
    unparseable record is not the same claim as "zero evidence," and
    AGENTS.md's missing-data-is-not-zero rule applies here as much as
    anywhere else in this project.
    """


@dataclass(frozen=True, slots=True)
class UnknownSource:
    """A source sending mail claiming the evaluated `header_from` domain
    that failed BOTH SPF and DKIM alignment -- unauthenticated traffic,
    not a false positive from a legitimate third-party sender whose
    alignment is merely imperfectly configured (those still pass on one
    mechanism or the other)."""

    source_identifier: str
    """`source.base_domain` when parsedmarc's reverse-DNS lookup resolved
    one, else the raw `source.ip_address` -- never dropped or merged with
    other domain-less sources just because a name wasn't resolvable."""
    message_count: int


@dataclass(frozen=True, slots=True)
class AuthHealthSummary:
    state: DataState
    total_messages: int
    aligned_messages: int
    unknown_sources: tuple[UnknownSource, ...]
    """Sorted by `message_count` descending, ties broken by identifier --
    the biggest unauthenticated sender first, since that's the one worth a
    human's attention first."""

    @property
    def aligned_rate(self) -> float | None:
        """`None` when `total_messages == 0` -- there is no rate to report
        from zero evidence, and `0.0` would misleadingly claim there is."""
        if self.total_messages == 0:
            return None
        return self.aligned_messages / self.total_messages


def summarize_auth_health(parsed_reports: Iterable[Mapping[str, object]]) -> AuthHealthSummary:
    """Aggregate one or more parsedmarc-parsed aggregate reports into an
    auth-health summary.

    `parsed_reports` being empty (or every report having zero records) is
    `INSUFFICIENT_DATA`, not a 0%-aligned or 100%-aligned result -- no
    reports arriving is common (a domain that sends little mail, or one no
    receiver has reported on yet) and must never be coerced into looking
    like evidence either way, same principle as `engine/state.py`'s
    handling of a missing Postmaster row.
    """
    total_messages = 0
    aligned_messages = 0
    unknown_counts: dict[str, int] = {}

    for report in parsed_reports:
        for record in _require_records(report):
            count = _record_count(record)
            total_messages += count
            if _record_is_aligned(record):
                aligned_messages += count
            else:
                source_id = _source_identifier(record)
                unknown_counts[source_id] = unknown_counts.get(source_id, 0) + count

    if total_messages == 0:
        return AuthHealthSummary(
            state=DataState.INSUFFICIENT_DATA,
            total_messages=0,
            aligned_messages=0,
            unknown_sources=(),
        )

    unknown_sources = tuple(
        sorted(
            (
                UnknownSource(source_identifier=source_id, message_count=count)
                for source_id, count in unknown_counts.items()
            ),
            key=lambda u: (-u.message_count, u.source_identifier),
        )
    )
    return AuthHealthSummary(
        state=DataState.OK,
        total_messages=total_messages,
        aligned_messages=aligned_messages,
        unknown_sources=unknown_sources,
    )


def _require_records(report: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw_records = report.get("records")
    if not isinstance(raw_records, list):
        raise MalformedDmarcReportError(
            f"report is missing a 'records' list, got {type(raw_records).__name__}"
        )
    records = cast(list[object], raw_records)
    result: list[Mapping[str, object]] = []
    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            raise MalformedDmarcReportError(
                f"record is not a mapping, got {type(raw_record).__name__}"
            )
        result.append(cast(Mapping[str, object], raw_record))
    return result


def _record_count(record: Mapping[str, object]) -> int:
    count = record.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise MalformedDmarcReportError(f"record 'count' must be an integer, got {count!r}")
    return count


def _record_is_aligned(record: Mapping[str, object]) -> bool:
    raw_alignment = record.get("alignment")
    if not isinstance(raw_alignment, Mapping):
        raise MalformedDmarcReportError(
            f"record is missing an 'alignment' mapping, got {type(raw_alignment).__name__}"
        )
    alignment = cast(Mapping[str, object], raw_alignment)
    return bool(alignment.get("spf")) or bool(alignment.get("dkim"))


def _source_identifier(record: Mapping[str, object]) -> str:
    raw_source = record.get("source")
    if not isinstance(raw_source, Mapping):
        raise MalformedDmarcReportError(
            f"record is missing a 'source' mapping, got {type(raw_source).__name__}"
        )
    source = cast(Mapping[str, object], raw_source)
    base_domain = source.get("base_domain")
    if isinstance(base_domain, str) and base_domain:
        return base_domain
    ip_address = source.get("ip_address")
    if isinstance(ip_address, str) and ip_address:
        return ip_address
    raise MalformedDmarcReportError(
        "record 'source' has neither a usable 'base_domain' nor 'ip_address'"
    )
