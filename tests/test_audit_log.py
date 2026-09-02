"""Tests for audit/log.py: serialization round-trip and log replay."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from deliverability_guard.audit.log import (
    CorruptDecisionRecordError,
    DecisionLogWriteError,
    DecisionRecord,
    ResumeRecord,
    append_record,
    append_resume_record,
    read_events,
    read_records,
    replay,
)
from deliverability_guard.engine.breaker import (
    DEFAULT_LADDER,
    BreakerStateStore,
    Verdict,
    evaluate,
)
from deliverability_guard.engine.posterior import DEFAULT_PRIOR
from deliverability_guard.engine.state import DataState
from deliverability_guard.providers.base import ActionOutcome, Capability, MailboxRef
from fixtures.fake_driver import FakeDriver

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_MAILBOX = MailboxRef(provider="fake", mailbox_id="a@example.com")


def _record_with_action() -> DecisionRecord:
    evaluation = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    assert evaluation.verdict == Verdict.PAUSE
    return DecisionRecord.from_evaluation(evaluation)


def _record_insufficient_data() -> DecisionRecord:
    evaluation = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=0,
        complaints=0,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    return DecisionRecord.from_evaluation(evaluation)


def test_from_evaluation_captures_the_pause_verdict_and_action() -> None:
    """`_record_with_action()` evaluates with `dry_run=True` -- the recorded
    `action_outcome` is `DRY_RUN`, not `PERFORMED`: CLOSE-4b, the record must
    never claim a real provider call happened when dry-run means it didn't.
    (`BreakerEvaluation.action.outcome` itself, at the engine level, stays
    PERFORMED either way -- see `providers.base.ActionOutcome.DRY_RUN`.)"""
    record = _record_with_action()
    assert record.verdict == Verdict.PAUSE
    assert record.data_state == DataState.OK
    assert record.action_outcome == ActionOutcome.DRY_RUN
    assert record.action_capability == Capability.PAUSE
    assert record.posterior_alpha is not None


def test_from_evaluation_of_a_live_pause_records_performed() -> None:
    """The live (non-dry-run) counterpart to the test above: a real action
    is recorded as PERFORMED, exactly as before -- the DRY_RUN distinction
    only ever applies to a dry-run evaluation."""
    evaluation = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=False,
        now=_NOW,
    )
    record = DecisionRecord.from_evaluation(evaluation)
    assert record.action_outcome == ActionOutcome.PERFORMED


def test_from_evaluation_handles_insufficient_data_with_no_posterior() -> None:
    record = _record_insufficient_data()
    assert record.data_state == DataState.INSUFFICIENT_DATA
    assert record.posterior_alpha is None
    assert record.posterior_beta is None
    assert record.action_outcome is None


def test_to_dict_from_dict_round_trip() -> None:
    original = _record_with_action()
    round_tripped = DecisionRecord.from_dict(original.to_dict())
    assert round_tripped == original


def test_to_dict_from_dict_round_trip_with_no_action() -> None:
    original = _record_insufficient_data()
    round_tripped = DecisionRecord.from_dict(original.to_dict())
    assert round_tripped == original


def test_to_dict_is_json_serializable() -> None:
    import json

    original = _record_with_action()
    json.dumps(original.to_dict())  # must not raise


def test_from_dict_rejects_a_missing_field() -> None:
    original = _record_with_action()
    data = original.to_dict()
    del data["sends"]
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


def test_from_dict_rejects_a_wrong_type() -> None:
    original = _record_with_action()
    data = original.to_dict()
    data["sends"] = "not a number"
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


def test_from_dict_rejects_an_invalid_enum_value() -> None:
    original = _record_with_action()
    data = original.to_dict()
    data["verdict"] = "NOT_A_REAL_VERDICT"
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


def test_from_dict_rejects_a_missing_required_number() -> None:
    original = _record_with_action()
    data = original.to_dict()
    del data["prior_alpha"]
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


def test_from_dict_rejects_a_non_numeric_required_number() -> None:
    original = _record_with_action()
    data = original.to_dict()
    data["prior_alpha"] = "not a number"
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


def test_from_dict_rejects_a_non_numeric_optional_number() -> None:
    original = _record_with_action()
    data = original.to_dict()
    data["posterior_alpha"] = "not a number"
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


def test_from_dict_rejects_a_non_string_optional_field() -> None:
    original = _record_with_action()
    data = original.to_dict()
    data["action_detail"] = 12345
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


def test_from_dict_rejects_a_non_string_optional_enum_field() -> None:
    original = _record_with_action()
    data = original.to_dict()
    data["action_outcome"] = 12345
    with pytest.raises(CorruptDecisionRecordError):
        DecisionRecord.from_dict(data)


# --- Replay ------------------------------------------------------------


def test_replay_reproduces_the_pause_verdict() -> None:
    record = _record_with_action()
    assert replay(record) == record.verdict == Verdict.PAUSE


def test_replay_of_insufficient_data_is_ok() -> None:
    record = _record_insufficient_data()
    assert replay(record) == Verdict.OK == record.verdict


def test_replay_reproduces_every_rung_of_the_ladder() -> None:
    cases = [
        (50, 1, Verdict.OK),
        (20_000, 20, Verdict.WARN),
        (20_000, 30, Verdict.THROTTLE),
        (5000, 40, Verdict.PAUSE),
    ]
    for sends, complaints, expected in cases:
        evaluation = evaluate(
            driver=FakeDriver(),
            mailbox=_MAILBOX,
            sends=sends,
            complaints=complaints,
            prior=DEFAULT_PRIOR,
            thresholds=DEFAULT_LADDER,
            state_store=BreakerStateStore(),
            dry_run=True,
            now=_NOW,
        )
        record = DecisionRecord.from_evaluation(evaluation)
        assert evaluation.verdict == expected, (sends, complaints)
        assert replay(record) == expected == record.verdict


# --- File persistence ----------------------------------------------------


def test_append_and_read_round_trips_multiple_records(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    records = [_record_with_action(), _record_insufficient_data(), _record_with_action()]
    for record in records:
        append_record(path, record)

    read_back = read_records(path)
    assert read_back == records


def test_read_records_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    record = _record_with_action()
    append_record(path, record)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n")  # a stray blank line, e.g. from manual editing
    append_record(path, record)
    assert read_records(path) == [record, record]


def test_read_records_from_empty_file_is_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    assert read_records(path) == []


def test_the_whole_evaluation_is_reproducible_from_the_log_alone(tmp_path: Path) -> None:
    """The end-to-end requirement: write a real evaluation to a log file,
    read it back with nothing but the file, and reproduce the same verdict
    -- using only `replay()`, never the original in-memory objects."""
    path = tmp_path / "decisions.jsonl"
    evaluation = evaluate(
        driver=FakeDriver(),
        mailbox=_MAILBOX,
        sends=5000,
        complaints=40,
        prior=DEFAULT_PRIOR,
        thresholds=DEFAULT_LADDER,
        state_store=BreakerStateStore(),
        dry_run=True,
        now=_NOW,
    )
    append_record(path, DecisionRecord.from_evaluation(evaluation))

    del evaluation  # prove we're only using what came back from disk

    (record,) = read_records(path)
    assert replay(record) == record.verdict == Verdict.PAUSE


# --- ResumeRecord --------------------------------------------------------


def _resume_record() -> ResumeRecord:
    return ResumeRecord(
        resumed_at=_NOW, provider="fake", mailbox_id="a@example.com", resumed_by="kate"
    )


def test_resume_record_to_dict_from_dict_round_trip() -> None:
    original = _resume_record()
    round_tripped = ResumeRecord.from_dict(original.to_dict())
    assert round_tripped == original


def test_resume_record_from_dict_rejects_a_missing_field() -> None:
    data = _resume_record().to_dict()
    del data["resumed_by"]
    with pytest.raises(CorruptDecisionRecordError):
        ResumeRecord.from_dict(data)


def test_read_records_skips_resume_records(tmp_path: Path) -> None:
    """`read_records` (decision records only) must not choke on -- or
    include -- a resume record interleaved in the same log."""
    path = tmp_path / "decisions.jsonl"
    decision = _record_with_action()
    append_record(path, decision)
    append_resume_record(path, _resume_record())
    append_record(path, decision)
    assert read_records(path) == [decision, decision]


def test_read_events_returns_decision_and_resume_records_in_file_order(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    decision = _record_with_action()
    resume = _resume_record()
    append_record(path, decision)
    append_resume_record(path, resume)

    events = read_events(path)

    assert events == [decision, resume]


def test_read_records_rejects_a_non_object_line(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    path.write_text("5\n")
    with pytest.raises(CorruptDecisionRecordError):
        read_records(path)


def test_read_events_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    decision = _record_with_action()
    append_record(path, decision)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n")
    append_resume_record(path, _resume_record())

    assert read_events(path) == [decision, _resume_record()]


# --- CLOSE11-1: append_record/append_resume_record raise a dedicated type
# on their own write failure, not a bare OSError ---------------------------


def test_append_record_wraps_a_real_write_failure_in_decision_log_write_error(
    tmp_path: Path,
) -> None:
    """A path whose parent directory doesn't exist makes `Path.open("a")`
    raise `FileNotFoundError` -- a real `OSError`, no monkeypatching. This
    is `append_record`'s own contract (CLOSE11-1): wrap it into
    `DecisionLogWriteError` so `cli.py`'s `check`/`run` handlers can catch
    that specific type instead of a bare `OSError`, which would also catch
    an unrelated provider-side socket error (see `tests/test_cli.py`'s
    CLOSE11-1 tests for why that distinction matters)."""
    path = tmp_path / "no-such-directory" / "decisions.jsonl"
    with pytest.raises(DecisionLogWriteError):
        append_record(path, _record_with_action())


def test_append_resume_record_wraps_a_real_write_failure_in_decision_log_write_error(
    tmp_path: Path,
) -> None:
    """Same property, `append_resume_record`."""
    path = tmp_path / "no-such-directory" / "decisions.jsonl"
    with pytest.raises(DecisionLogWriteError):
        append_resume_record(path, _resume_record())
