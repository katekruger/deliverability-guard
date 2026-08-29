"""Data-availability state machine: OK | INSUFFICIENT_DATA | STALE.

Google omits low-volume days from Postmaster Tools entirely -- an unpublished
privacy threshold, community-estimated around 50-100 sends/day (BUILD-PLAN.md
§8). Absence of a row is common, not exceptional, and it must never be read
as "nothing bad happened." Worse: a domain that gets throttled sends less,
can drop below that threshold as a direct consequence of the throttle, and
disappear from reporting entirely -- monitoring goes dark exactly when things
are worst. These three states exist to make that failure mode structurally
impossible to collapse into "OK":

  OK                 Real data was reported, however small.
  INSUFFICIENT_DATA  No data available -- never seen, or an explicit zero-row.
  STALE              Data WAS available and has just stopped arriving. This
                      is not merely INSUFFICIENT_DATA continued -- it is a
                      transition, and the transition itself is the alert.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum, auto


class DataState(Enum):
    """First-class and never collapsed into one another. In particular,
    INSUFFICIENT_DATA is never coerced to OK, and a STALE transition is never
    silently absorbed into ongoing INSUFFICIENT_DATA."""

    OK = auto()
    INSUFFICIENT_DATA = auto()
    STALE = auto()


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
