"""Tests for signals/dmarc.py -- auth-health summarization built on
parsedmarc's own parsed-report output (BUILD-PLAN.md §4 item #17: "Reuse
as a library. Do not reimplement."). Most tests feed fabricated dicts
shaped like parsedmarc's documented output; one integration test below
calls parsedmarc's real XML parser with `offline=True` (no reverse-DNS or
GeoIP network lookups) to confirm the assumed shape actually matches --
still no live network call of any kind."""

import parsedmarc
import pytest

from deliverability_guard.engine.state import DataState
from deliverability_guard.signals.dmarc import (
    MalformedDmarcReportError,
    summarize_auth_health,
)

_SAMPLE_AGGREGATE_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<feedback>
  <report_metadata>
    <org_name>google.com</org_name>
    <email>noreply-dmarc-support@google.com</email>
    <report_id>1234567890</report_id>
    <date_range><begin>1609459200</begin><end>1609545600</end></date_range>
  </report_metadata>
  <policy_published>
    <domain>example.com</domain>
    <adkim>r</adkim>
    <aspf>r</aspf>
    <p>none</p>
    <sp>none</sp>
    <pct>100</pct>
  </policy_published>
  <record>
    <row>
      <source_ip>203.0.113.10</source_ip>
      <count>5</count>
      <policy_evaluated><disposition>none</disposition><dkim>pass</dkim><spf>pass</spf></policy_evaluated>
    </row>
    <identifiers><header_from>example.com</header_from></identifiers>
    <auth_results><spf><domain>example.com</domain><result>pass</result></spf></auth_results>
  </record>
  <record>
    <row>
      <source_ip>198.51.100.99</source_ip>
      <count>3</count>
      <policy_evaluated><disposition>none</disposition><dkim>fail</dkim><spf>fail</spf></policy_evaluated>
    </row>
    <identifiers><header_from>example.com</header_from></identifiers>
    <auth_results><spf><domain>unknown-sender.net</domain><result>fail</result></spf></auth_results>
  </record>
</feedback>"""


def test_integration_with_parsedmarcs_real_xml_parser_offline() -> None:
    """Confirms this module's assumptions about parsedmarc's output shape
    against parsedmarc's actual parser, not just a hand-fabricated stand-in
    -- `offline=True` guarantees no reverse-DNS or GeoIP network lookup is
    attempted, so this stays a deterministic, network-free test."""
    parsed = parsedmarc.parse_aggregate_report_xml(  # pyright: ignore[reportUnknownMemberType]
        _SAMPLE_AGGREGATE_XML, offline=True
    )
    summary = summarize_auth_health([parsed])

    assert summary.state is DataState.OK
    assert summary.total_messages == 8
    assert summary.aligned_messages == 5
    # offline=True means no reverse DNS -> base_domain is None -> falls
    # back to the raw IP, exactly the fallback this module implements.
    assert len(summary.unknown_sources) == 1
    assert summary.unknown_sources[0].source_identifier == "198.51.100.99"
    assert summary.unknown_sources[0].message_count == 3


def _report(records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "report_metadata": {"org_name": "google.com", "report_id": "123"},
        "records": records,
    }


def _record(
    *,
    count: int,
    spf_aligned: bool,
    dkim_aligned: bool,
    base_domain: str = "unknown-sender.example",
) -> dict[str, object]:
    return {
        "source": {"ip_address": "203.0.113.10", "base_domain": base_domain},
        "count": count,
        "alignment": {"spf": spf_aligned, "dkim": dkim_aligned},
        "identifiers": {"header_from": "example.com"},
    }


def test_no_reports_is_insufficient_data() -> None:
    summary = summarize_auth_health([])
    assert summary.state is DataState.INSUFFICIENT_DATA
    assert summary.total_messages == 0
    assert summary.aligned_rate is None
    assert summary.unknown_sources == ()


def test_all_aligned_messages_have_no_unknown_sources() -> None:
    report = _report([_record(count=10, spf_aligned=True, dkim_aligned=True)])
    summary = summarize_auth_health([report])
    assert summary.state is DataState.OK
    assert summary.total_messages == 10
    assert summary.aligned_messages == 10
    assert summary.aligned_rate == 1.0
    assert summary.unknown_sources == ()


def test_spf_only_alignment_still_counts_as_aligned() -> None:
    """DMARC passes on EITHER SPF or DKIM alignment, not both (RFC 9989) --
    a record aligned on SPF alone must not be misclassified as unknown."""
    report = _report([_record(count=5, spf_aligned=True, dkim_aligned=False)])
    summary = summarize_auth_health([report])
    assert summary.aligned_messages == 5
    assert summary.unknown_sources == ()


def test_dkim_only_alignment_still_counts_as_aligned() -> None:
    report = _report([_record(count=5, spf_aligned=False, dkim_aligned=True)])
    summary = summarize_auth_health([report])
    assert summary.aligned_messages == 5
    assert summary.unknown_sources == ()


def test_a_record_failing_both_is_an_unknown_source() -> None:
    report = _report(
        [_record(count=7, spf_aligned=False, dkim_aligned=False, base_domain="spoofer.example")]
    )
    summary = summarize_auth_health([report])
    assert summary.state is DataState.OK
    assert summary.total_messages == 7
    assert summary.aligned_messages == 0
    assert summary.aligned_rate == 0.0
    assert len(summary.unknown_sources) == 1
    assert summary.unknown_sources[0].source_identifier == "spoofer.example"
    assert summary.unknown_sources[0].message_count == 7


def test_unknown_sources_are_aggregated_across_records_and_reports() -> None:
    report_a = _report(
        [_record(count=3, spf_aligned=False, dkim_aligned=False, base_domain="spoofer.example")]
    )
    report_b = _report(
        [_record(count=4, spf_aligned=False, dkim_aligned=False, base_domain="spoofer.example")]
    )
    summary = summarize_auth_health([report_a, report_b])
    assert len(summary.unknown_sources) == 1
    assert summary.unknown_sources[0].message_count == 7


def test_unknown_sources_are_sorted_by_message_count_descending() -> None:
    report = _report(
        [
            _record(count=2, spf_aligned=False, dkim_aligned=False, base_domain="small.example"),
            _record(count=50, spf_aligned=False, dkim_aligned=False, base_domain="big.example"),
        ]
    )
    summary = summarize_auth_health([report])
    assert [s.source_identifier for s in summary.unknown_sources] == [
        "big.example",
        "small.example",
    ]


def test_unknown_source_falls_back_to_ip_address_when_base_domain_is_missing() -> None:
    """Not every source has a resolvable reverse-DNS base domain --
    parsedmarc leaves `base_domain` absent/None in that case. Falling back
    to the raw IP keeps the source identifiable rather than silently
    dropping or merging it with every other domain-less source."""
    record = _record(count=1, spf_aligned=False, dkim_aligned=False)
    record["source"] = {"ip_address": "198.51.100.7", "base_domain": None}
    summary = summarize_auth_health([_report([record])])
    assert summary.unknown_sources[0].source_identifier == "198.51.100.7"


def test_mixed_aligned_and_unknown_records_in_one_report() -> None:
    report = _report(
        [
            _record(count=100, spf_aligned=True, dkim_aligned=True),
            _record(count=5, spf_aligned=False, dkim_aligned=False, base_domain="spoofer.example"),
        ]
    )
    summary = summarize_auth_health([report])
    assert summary.total_messages == 105
    assert summary.aligned_messages == 100
    assert summary.aligned_rate == pytest.approx(100 / 105)
    assert len(summary.unknown_sources) == 1


def test_raises_when_a_record_in_the_list_is_not_a_mapping() -> None:
    with pytest.raises(MalformedDmarcReportError, match="not a mapping"):
        summarize_auth_health([_report(["not-a-record"])])  # type: ignore[list-item]


def test_raises_when_a_record_is_missing_source() -> None:
    record = _record(count=1, spf_aligned=False, dkim_aligned=False)
    del record["source"]
    with pytest.raises(MalformedDmarcReportError, match="source"):
        summarize_auth_health([_report([record])])


def test_raises_when_source_has_neither_base_domain_nor_ip_address() -> None:
    record = _record(count=1, spf_aligned=False, dkim_aligned=False)
    record["source"] = {}
    with pytest.raises(MalformedDmarcReportError, match="base_domain"):
        summarize_auth_health([_report([record])])


def test_raises_when_records_key_is_missing() -> None:
    with pytest.raises(MalformedDmarcReportError, match="records"):
        summarize_auth_health([{"report_metadata": {}}])


def test_raises_when_a_record_is_missing_alignment() -> None:
    report = _report([{"source": {"base_domain": "x"}, "count": 1}])
    with pytest.raises(MalformedDmarcReportError, match="alignment"):
        summarize_auth_health([report])


def test_raises_when_count_is_not_an_integer() -> None:
    record = _record(count=5, spf_aligned=True, dkim_aligned=True)
    record["count"] = "five"
    with pytest.raises(MalformedDmarcReportError, match="count"):
        summarize_auth_health([_report([record])])
