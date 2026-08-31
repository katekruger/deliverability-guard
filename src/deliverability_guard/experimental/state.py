"""`DailyReport`/`classify`/`StateEvaluation`/`evaluate_stream`: the
multi-day STALE-transition state machine built on top of
`engine.state.DataState`.

STATUS (CLOSE3-5): moved here from `engine/state.py`, mirroring
`experimental.postmaster_coverage`'s own move (CLOSE-1) and for the same
reason. This machinery's only production-side caller is
`experimental.postmaster_coverage.coverage_over_range`, itself experimental
because no Postmaster domain-stats ingestion pipeline exists anywhere in
this codebase yet. A production function whose sole consumer is
non-production belongs on one side or the other -- `DataState` alone stays
in `engine/state.py` on the strength of `engine.breaker`/`audit.log`'s real
usage; this multi-day machinery moves with its only real caller.

Promote this back to `engine/state.py` once a real Postmaster (or other
multi-day-reporting) ingestion pipeline calls `evaluate_stream` from
production code.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from deliverability_guard.engine.state import DataState


@dataclass(frozen=True, slots=True)
class DailyReport:
    """One day's reported sends/complaints for a mailbox or domain.

    `sends` is `None` when the upstream provider reported no row at all for
    this day -- e.g. a day below Postmaster's privacy threshold. That is a
    different situation from a row that explicitly reports 0 sends (the
    provider is reachable and says nothing happened), but both are
    INSUFFICIENT_DATA here: in neither case is there evidence to say
    anything about a rate.
    """

    day: date
    sends: int | None
    complaints: int | None

    @property
    def has_data(self) -> bool:
        return self.sends is not None and self.sends > 0


def classify(report: DailyReport) -> DataState:
    """Classify a single day in isolation.

    A single day can only ever be OK or INSUFFICIENT_DATA -- STALE requires
    history (see `evaluate_stream`), because it's defined by a transition,
    not by any single day's contents.
    """
    if report.has_data:
        return DataState.OK
    return DataState.INSUFFICIENT_DATA


@dataclass(frozen=True, slots=True)
class StateEvaluation:
    day: date
    state: DataState
    transition_alert: bool
    """True exactly on the day data availability transitions from present to
    absent. Only that first day carries the alert -- further days of
    continued absence are ordinary INSUFFICIENT_DATA, not repeated alarms.
    The alert models the transition itself as the event worth flagging."""


def evaluate_stream(reports: Sequence[DailyReport]) -> list[StateEvaluation]:
    """Evaluate a chronologically-ordered sequence of daily reports."""
    evaluations: list[StateEvaluation] = []
    previously_had_data = False
    for report in reports:
        currently_has_data = report.has_data
        if currently_has_data:
            state = DataState.OK
            transition_alert = False
        elif previously_had_data:
            state = DataState.STALE
            transition_alert = True
        else:
            state = DataState.INSUFFICIENT_DATA
            transition_alert = False
        evaluations.append(
            StateEvaluation(day=report.day, state=state, transition_alert=transition_alert)
        )
        previously_had_data = currently_has_data
    return evaluations
