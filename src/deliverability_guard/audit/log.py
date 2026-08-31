"""The decision log.

Every evaluation records: timestamp, mailbox, all inputs (sends,
complaints), the posterior and its bounds, data-availability state, the
threshold ladder in force, the verdict, the action attempted (if any), and
the provider's response (if any) -- exactly the fields BUILD-PLAN.md §5
requires, and no more, so a `DecisionRecord` is safe to write to a log file
verbatim: it does not carry credentials, full request URLs, or anything
else `docs/threat-model.md` flags as sensitive.

The requirement this module exists to satisfy: the whole evaluation must be
reproducible from the log alone. `replay()` recomputes a verdict from a
record's own stored inputs using the same engine functions
`engine.breaker.evaluate` uses internally -- if that doesn't match the
verdict actually stored on the record, either the record was tampered with
or the engine's logic changed incompatibly since it was written, and either
is worth knowing.

(Deliberately the same shape as the agent-audit project's
proposed/decided/executed record. They should converge in v0.3.)
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import cast

from deliverability_guard.engine.breaker import BreakerEvaluation, ThresholdLadder, Verdict, rung
from deliverability_guard.engine.posterior import BetaDistribution, update
from deliverability_guard.engine.state import DataState
from deliverability_guard.providers.base import ActionOutcome, Capability


class CorruptDecisionRecordError(Exception):
    """A serialized decision record couldn't be parsed back into a `DecisionRecord`."""


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """A JSON-serializable snapshot of one `BreakerEvaluation`."""

    evaluated_at: datetime
    provider: str
    mailbox_id: str
    sends: int
    complaints: int
    data_state: DataState
    prior_alpha: float
    prior_beta: float
    posterior_alpha: float | None
    posterior_beta: float | None
    lower_bound: float | None
    confidence: float
    threshold_warn: float
    threshold_throttle: float
    threshold_pause: float
    verdict: Verdict
    dry_run: bool
    action_outcome: ActionOutcome | None
    action_detail: str | None
    action_capability: Capability | None
    applied_daily_limit: int | None = None
    """Persists `BreakerEvaluation.applied_daily_limit` (CLOSE3-1): the
    `current_daily_limit` input a PERFORMED throttle is currently locked to.
    `BreakerStateStore.from_log` restores `_throttled_at_limit` from this,
    which is what keeps a process that restarts between every evaluation
    (e.g. `check` run from cron) idempotent from its very next re-evaluation
    instead of re-halving on every single restart."""

    @classmethod
    def from_evaluation(cls, evaluation: BreakerEvaluation) -> "DecisionRecord":
        posterior = evaluation.posterior
        action = evaluation.action
        action_outcome = action.outcome if action is not None else None
        if evaluation.dry_run and action_outcome is ActionOutcome.PERFORMED:
            # The engine's own `action.outcome` stays PERFORMED for a
            # dry-run action -- AGENTS.md requires dry-run decisions be
            # identical to the live path, and `engine.breaker._act` relies
            # on that to keep its idempotency logic dry-run/live agnostic.
            # The persisted LOG record is a different contract: its job is
            # to tell a human, or `replay()`, what actually happened in the
            # world, and a dry-run action never touched the real provider.
            # Recording it as PERFORMED here (and nowhere else) is exactly
            # the "log claims something happened that didn't" bug this
            # distinction exists to close.
            action_outcome = ActionOutcome.DRY_RUN
        return cls(
            evaluated_at=evaluation.evaluated_at,
            provider=evaluation.mailbox.provider,
            mailbox_id=evaluation.mailbox.mailbox_id,
            sends=evaluation.sends,
            complaints=evaluation.complaints,
            data_state=evaluation.data_state,
            prior_alpha=evaluation.prior.alpha,
            prior_beta=evaluation.prior.beta,
            posterior_alpha=posterior.alpha if posterior is not None else None,
            posterior_beta=posterior.beta if posterior is not None else None,
            lower_bound=evaluation.lower_bound,
            confidence=evaluation.confidence,
            threshold_warn=evaluation.thresholds.warn,
            threshold_throttle=evaluation.thresholds.throttle,
            threshold_pause=evaluation.thresholds.pause,
            verdict=evaluation.verdict,
            dry_run=evaluation.dry_run,
            action_outcome=action_outcome,
            action_detail=action.detail if action is not None else None,
            action_capability=action.capability if action is not None else None,
            applied_daily_limit=evaluation.applied_daily_limit,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "decision",
            "evaluated_at": self.evaluated_at.astimezone(UTC).isoformat(),
            "provider": self.provider,
            "mailbox_id": self.mailbox_id,
            "sends": self.sends,
            "complaints": self.complaints,
            "data_state": self.data_state.name,
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
            "posterior_alpha": self.posterior_alpha,
            "posterior_beta": self.posterior_beta,
            "lower_bound": self.lower_bound,
            "confidence": self.confidence,
            "threshold_warn": self.threshold_warn,
            "threshold_throttle": self.threshold_throttle,
            "threshold_pause": self.threshold_pause,
            "verdict": self.verdict.name,
            "dry_run": self.dry_run,
            "action_outcome": self.action_outcome.name if self.action_outcome is not None else None,
            "action_detail": self.action_detail,
            "action_capability": (
                self.action_capability.name if self.action_capability is not None else None
            ),
            "applied_daily_limit": self.applied_daily_limit,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "DecisionRecord":
        try:
            return cls(
                evaluated_at=datetime.fromisoformat(_require(data, "evaluated_at", str)),
                provider=_require(data, "provider", str),
                mailbox_id=_require(data, "mailbox_id", str),
                sends=_require(data, "sends", int),
                complaints=_require(data, "complaints", int),
                data_state=DataState[_require(data, "data_state", str)],
                prior_alpha=_require_number(data, "prior_alpha"),
                prior_beta=_require_number(data, "prior_beta"),
                posterior_alpha=_optional_number(data, "posterior_alpha"),
                posterior_beta=_optional_number(data, "posterior_beta"),
                lower_bound=_optional_number(data, "lower_bound"),
                confidence=_require_number(data, "confidence"),
                threshold_warn=_require_number(data, "threshold_warn"),
                threshold_throttle=_require_number(data, "threshold_throttle"),
                threshold_pause=_require_number(data, "threshold_pause"),
                verdict=Verdict[_require(data, "verdict", str)],
                dry_run=_require(data, "dry_run", bool),
                action_outcome=_optional_enum(data, "action_outcome", ActionOutcome),
                action_detail=_optional(data, "action_detail", str),
                action_capability=_optional_enum(data, "action_capability", Capability),
                applied_daily_limit=_optional(data, "applied_daily_limit", int),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise CorruptDecisionRecordError(f"could not parse decision record: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ResumeRecord:
    """A `resume_after_human_review` event, logged so it survives a restart.

    Not a `BreakerEvaluation` -- there's no posterior, verdict, or action to
    it, just "a human resumed this mailbox, and here's who and when." Written
    to the same JSONL file as `DecisionRecord`s, distinguished by the "kind"
    key, and replayed in file order by `engine.breaker.BreakerStateStore.
    from_log` alongside decision records so a resume that happened between
    two evaluations is reflected correctly (ADR 0003's "known limitation":
    before this existed, a resumed-then-never-re-evaluated mailbox rebuilt
    as PAUSED after a restart, silently losing the human's action).
    """

    resumed_at: datetime
    provider: str
    mailbox_id: str
    resumed_by: str

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "resume",
            "resumed_at": self.resumed_at.astimezone(UTC).isoformat(),
            "provider": self.provider,
            "mailbox_id": self.mailbox_id,
            "resumed_by": self.resumed_by,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ResumeRecord":
        try:
            return cls(
                resumed_at=datetime.fromisoformat(_require(data, "resumed_at", str)),
                provider=_require(data, "provider", str),
                mailbox_id=_require(data, "mailbox_id", str),
                resumed_by=_require(data, "resumed_by", str),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise CorruptDecisionRecordError(f"could not parse resume record: {exc}") from exc


def replay(record: DecisionRecord) -> Verdict:
    """Recompute the verdict from a record's own stored inputs.

    This deliberately does NOT read `record.verdict` to decide what to
    return -- it is a from-scratch recomputation using the same
    `engine.posterior.update` and `engine.breaker.rung` the original
    evaluation used, so that comparing the result against `record.verdict`
    is a real reproducibility check, not a tautology.
    """
    if record.data_state is not DataState.OK:
        return Verdict.OK
    prior = BetaDistribution(alpha=record.prior_alpha, beta=record.prior_beta)
    posterior = update(prior, record.sends, record.complaints)
    lower_bound = posterior.lower_bound(record.confidence)
    thresholds = ThresholdLadder(
        warn=record.threshold_warn,
        throttle=record.threshold_throttle,
        pause=record.threshold_pause,
    )
    return rung(lower_bound, thresholds)


def append_record(path: Path, record: DecisionRecord) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict()) + "\n")


def append_resume_record(path: Path, record: ResumeRecord) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict()) + "\n")


def _is_resume_line(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    typed_data = cast("dict[str, object]", data)
    return typed_data.get("kind") == "resume"


def read_records(path: Path) -> list[DecisionRecord]:
    """Decision records only, in file order -- resume records (see
    `read_events` for both kinds together) are silently skipped rather than
    raising, so this stays a valid read of a log that predates resume
    records ever being written to it."""
    records: list[DecisionRecord] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            if _is_resume_line(data):
                continue
            records.append(DecisionRecord.from_dict(data))
    return records


def read_events(path: Path) -> list[DecisionRecord | ResumeRecord]:
    """Every record in the log, in file order, decision and resume events
    interleaved -- what `engine.breaker.BreakerStateStore.from_log` needs to
    replay state correctly, since a resume that happened between two
    decisions must be applied at the right point in the sequence, not
    lumped in before or after it."""
    events: list[DecisionRecord | ResumeRecord] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            if _is_resume_line(data):
                events.append(ResumeRecord.from_dict(data))
            else:
                events.append(DecisionRecord.from_dict(data))
    return events


def _require[T](data: Mapping[str, object], key: str, expected_type: type[T]) -> T:
    if key not in data:
        raise KeyError(key)
    value = data[key]
    if not isinstance(value, expected_type):
        raise TypeError(f"'{key}' must be {expected_type.__name__}, got {type(value).__name__}")
    return value


def _require_number(data: Mapping[str, object], key: str) -> float:
    if key not in data:
        raise KeyError(key)
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"'{key}' must be a number, got {type(value).__name__}")
    return float(value)


def _optional[T](data: Mapping[str, object], key: str, expected_type: type[T]) -> T | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, expected_type):
        raise TypeError(
            f"'{key}' must be {expected_type.__name__} or null, got {type(value).__name__}"
        )
    return value


def _optional_number(data: Mapping[str, object], key: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"'{key}' must be a number or null, got {type(value).__name__}")
    return float(value)


def _optional_enum[E: Enum](data: Mapping[str, object], key: str, enum_type: type[E]) -> E | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"'{key}' must be a string or null, got {type(value).__name__}")
    return enum_type[value]
