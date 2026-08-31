"""Data-availability state: OK | INSUFFICIENT_DATA | STALE.

Google omits low-volume days from Postmaster Tools entirely -- an unpublished
privacy threshold, community-estimated around 50-100 sends/day (BUILD-PLAN.md
§8). Absence of a row is common, not exceptional, and it must never be read
as "nothing bad happened." Worse: a domain that gets throttled sends less,
can drop below that threshold as a direct consequence of the throttle, and
disappear from reporting entirely -- monitoring goes dark exactly when things
are worst. `DataState` exists to make that failure mode structurally
impossible to collapse into "OK", and is what `engine.breaker.BreakerEvaluation.
data_state` and `audit.log.DecisionRecord.data_state` carry through the
project's real evaluation and persistence path.

`DailyReport`/`classify`/`StateEvaluation`/`evaluate_stream` -- the
multi-day STALE-transition state machine built on top of this enum -- used
to live here too. CLOSE3-5: they moved to `experimental.state` because
their only production-side caller, `experimental.postmaster_coverage.
coverage_over_range`, is itself experimental (no Postmaster domain-stats
ingestion pipeline exists in this codebase yet -- see that module's
docstring). A production function whose sole consumer is non-production
belongs on one side or the other; `DataState` alone earns its place in the
production package on the strength of `engine.breaker`/`audit.log`'s real
usage, but the multi-day machinery built on it does not, yet.
"""

from enum import Enum, auto


class DataState(Enum):
    """First-class and never collapsed into one another. In particular,
    INSUFFICIENT_DATA is never coerced to OK, and a STALE transition is never
    silently absorbed into ongoing INSUFFICIENT_DATA."""

    OK = auto()
    INSUFFICIENT_DATA = auto()
    STALE = auto()
