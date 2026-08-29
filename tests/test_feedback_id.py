"""Tests for identity/feedback_id.py."""

import pytest

from deliverability_guard.identity.feedback_id import (
    CoverageReport,
    FeedbackId,
    check_coverage,
    parse,
)


def test_encode_round_trips_through_parse() -> None:
    fid = FeedbackId(campaign="camp-1", segment="seg-a", mailbox="mbox-1", tenant="acme")
    assert parse(fid.encode()) == fid


def test_encode_produces_the_documented_scheme() -> None:
    fid = FeedbackId(campaign="camp", segment="seg", mailbox="mbox", tenant="tenant")
    assert fid.encode() == "camp:seg:mbox:tenant"


def test_rejects_an_empty_component() -> None:
    with pytest.raises(ValueError, match="campaign"):
        FeedbackId(campaign="", segment="seg", mailbox="mbox", tenant="tenant")


def test_rejects_a_component_with_unsafe_characters() -> None:
    with pytest.raises(ValueError, match="segment"):
        FeedbackId(campaign="camp", segment="seg with spaces", mailbox="mbox", tenant="tenant")


def test_parse_rejects_the_wrong_number_of_components() -> None:
    with pytest.raises(ValueError, match="4"):
        parse("only:three:parts")


def test_parse_rejects_too_many_components() -> None:
    with pytest.raises(ValueError, match="4"):
        parse("a:b:c:d:e")


def test_parse_propagates_component_validation() -> None:
    with pytest.raises(ValueError, match="safe set"):
        parse("camp:seg with spaces:mbox:tenant")


# --- check_coverage ---------------------------------------------------------


def test_check_coverage_all_present() -> None:
    report = check_coverage(["camp:seg:mbox:tenant", "camp2:seg:mbox:tenant"])
    assert report.total == 2
    assert report.with_feedback_id == 2
    assert report.invalid == ()
    assert report.percentage() == 1.0


def test_check_coverage_partial() -> None:
    """Feedback-ID present on some mail, absent on others -> report partial
    coverage with the percentage, don't silently under-attribute."""
    report = check_coverage(["camp:seg:mbox:tenant", None, None])
    assert report.total == 3
    assert report.with_feedback_id == 1
    assert report.percentage() == pytest.approx(1 / 3)


def test_check_coverage_counts_present_but_invalid_headers_separately() -> None:
    report = check_coverage(["camp:seg:mbox:tenant", "not-a-valid-header"])
    assert report.total == 2
    assert report.with_feedback_id == 1
    assert report.invalid == ("not-a-valid-header",)
    assert report.percentage() == 0.5


def test_check_coverage_empty_batch_percentage_is_none_not_zero() -> None:
    """No messages checked is not the same claim as 0% coverage."""
    report = check_coverage([])
    assert report.total == 0
    assert report.percentage() is None


def test_coverage_report_is_a_plain_dataclass_with_expected_fields() -> None:
    report = CoverageReport(total=5, with_feedback_id=3, invalid=("x",))
    assert report.total == 5
    assert report.with_feedback_id == 3
    assert report.invalid == ("x",)
