"""`coverage_over_range`: per-day data-availability tracking for a Postmaster
metric, over a date range.

STATUS (August 2026, CLOSE-1): moved here from `signals/postmaster.py`
because it has no production caller. Nothing in `cli.py` or `loops/`
ingests Postmaster domain-stats data at all -- `signals.postmaster` is
otherwise used only via `PostmasterClient.get_compliance_status` /
`forces_hard_gate`, which feed `engine.breaker.evaluate`'s
`compliance_gate_tripped` parameter, a genuinely wired (if not yet
CLI-invoked) integration point. `coverage_over_range` has no equivalent:
there is no code anywhere that calls `PostmasterClient.query_domain_stats`
and hands the result to this function. Building real production wiring for
it -- a Postmaster OAuth flow, a config schema for which domains/metrics to
poll, a CLI subcommand or a `loops/` integration point -- is real,
un-shipped v0.2 infrastructure (BUILD-PLAN.md §3), not a wiring gap this
project can close by threading one more parameter through an existing
chokepoint the way `engine.posterior.pooled_posterior` and
`engine.changepoint.cusum_step` were (see `loops/fast.py`'s
`evaluate_all_mailboxes`).

This module -- and the tests in `tests/experimental/test_postmaster_coverage.py`
-- exist so the underlying logic isn't lost or silently regressed while
that real integration is still unbuilt. Nothing in `src/deliverability_guard`
outside this module imports from here. Promote it back to `signals/postmaster.py`
(or wherever the real Postmaster ingestion pipeline ends up living) once
that pipeline exists and can call it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from deliverability_guard.engine.state import DataState
from deliverability_guard.experimental.state import DailyReport, evaluate_stream
from deliverability_guard.signals.postmaster import DomainStatRow


@dataclass(frozen=True, slots=True)
class DayAvailability:
    day: date
    state: DataState
    transition_alert: bool
    """True exactly on the day this metric transitions from having data to
    not having it. See `coverage_over_range` below."""


def coverage_over_range(
    rows: Sequence[DomainStatRow],
    *,
    metric_name: str,
    since: date,
    until: date,
) -> list[DayAvailability]:
    """Per-day OK/INSUFFICIENT_DATA/STALE for one metric over a date range,
    treating a present-to-absent transition as its own alert.

    This exists for the landmine BUILD-PLAN.md §8 and §9 both call out: a
    domain that gets throttled sends less, can drop below Postmaster's
    (unpublished) privacy threshold as a direct consequence, and disappear
    from `domainStats:query` results entirely -- monitoring goes dark
    exactly when things are worst. A missing day is never coerced to "0%,
    therefore healthy"; a transition into missing days is its own alert.

    Delegates the actual OK/INSUFFICIENT_DATA/STALE transition logic to
    `engine.state.evaluate_stream` rather than reimplementing it: a
    Postmaster row carries a bare rate value with no sends/complaints count,
    so it's translated into a placeholder `DailyReport` (`sends=1` when
    present, `sends=None` when absent) purely to signal presence/absence --
    `evaluate_stream` only ever looks at `DailyReport.has_data`, never the
    counts themselves, so the placeholder values are never a lossy stand-in
    for real data.
    """
    if since > until:
        raise ValueError(f"since ({since}) must not be after until ({until})")
    present_days = {
        row.day for row in rows if row.metric_name == metric_name and row.day is not None
    }
    reports: list[DailyReport] = []
    current = since
    one_day = timedelta(days=1)
    while current <= until:
        has_data = current in present_days
        reports.append(
            DailyReport(
                day=current, sends=1 if has_data else None, complaints=0 if has_data else None
            )
        )
        current += one_day
    return [
        DayAvailability(day=e.day, state=e.state, transition_alert=e.transition_alert)
        for e in evaluate_stream(reports)
    ]
