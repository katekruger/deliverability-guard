"""Tests for signals/spamhaus.py -- DNSBL lookups against the Spamhaus ZEN
zone (BUILD-PLAN.md §4 item #25). No live DNS lookups, ever: every test
injects a fake resolver, or (for `default_resolve` itself) monkeypatches
`socket.gethostbyname_ex` rather than hitting a real DNS server."""

import socket

import pytest

from deliverability_guard.signals.spamhaus import (
    DEFAULT_ZONE,
    DnsNameNotFoundError,
    SpamhausListing,
    SpamhausLookupError,
    check_ip,
    default_resolve,
)


def _resolver(answers: dict[str, list[str]]):
    def resolve(hostname: str) -> list[str]:
        if hostname not in answers:
            raise DnsNameNotFoundError(hostname)
        return answers[hostname]

    return resolve


def test_not_listed_ip_returns_not_listed() -> None:
    resolve = _resolver({})  # every query raises DnsNameNotFoundError
    result = check_ip("1.2.3.4", resolve=resolve)
    assert result.listing is SpamhausListing.NOT_LISTED
    assert result.raw_codes == ()


def test_query_is_the_reversed_octets_under_the_zone() -> None:
    seen: list[str] = []

    def resolve(hostname: str) -> list[str]:
        seen.append(hostname)
        raise DnsNameNotFoundError(hostname)

    check_ip("1.2.3.4", resolve=resolve)
    assert seen == [f"4.3.2.1.{DEFAULT_ZONE}"]


def test_a_custom_zone_is_used_verbatim() -> None:
    seen: list[str] = []

    def resolve(hostname: str) -> list[str]:
        seen.append(hostname)
        raise DnsNameNotFoundError(hostname)

    check_ip("1.2.3.4", zone="sbl.spamhaus.org", resolve=resolve)
    assert seen == ["4.3.2.1.sbl.spamhaus.org"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("127.0.0.2", SpamhausListing.SBL),
        ("127.0.0.3", SpamhausListing.CSS),
        ("127.0.0.4", SpamhausListing.XBL),
        ("127.0.0.5", SpamhausListing.XBL),
        ("127.0.0.6", SpamhausListing.XBL),
        ("127.0.0.7", SpamhausListing.XBL),
        ("127.0.0.10", SpamhausListing.PBL_ISP),
        ("127.0.0.11", SpamhausListing.PBL_SPAMHAUS),
    ],
)
def test_known_return_codes_map_to_the_right_listing(code: str, expected: SpamhausListing) -> None:
    resolve = _resolver({f"4.3.2.1.{DEFAULT_ZONE}": [code]})
    result = check_ip("1.2.3.4", resolve=resolve)
    assert result.listing is expected
    assert result.raw_codes == (code,)


def test_an_unrecognized_return_code_is_unknown_listed_not_silently_clean() -> None:
    """A return code this module doesn't recognize (e.g. Spamhaus adds a new
    one) must never be read as NOT_LISTED -- that would silently hide a real
    listing behind an unrecognized code."""
    resolve = _resolver({f"4.3.2.1.{DEFAULT_ZONE}": ["127.0.0.99"]})
    result = check_ip("1.2.3.4", resolve=resolve)
    assert result.listing is SpamhausListing.UNKNOWN_LISTED
    assert result.raw_codes == ("127.0.0.99",)


def test_multiple_return_codes_uses_the_first_and_keeps_all_raw_codes() -> None:
    resolve = _resolver({f"4.3.2.1.{DEFAULT_ZONE}": ["127.0.0.4", "127.0.0.10"]})
    result = check_ip("1.2.3.4", resolve=resolve)
    assert result.listing is SpamhausListing.XBL
    assert result.raw_codes == ("127.0.0.4", "127.0.0.10")


def test_a_genuine_lookup_failure_raises_rather_than_reading_as_not_listed() -> None:
    """A DNS timeout, SERVFAIL, or network error is not evidence the IP is
    clean -- the same missing-data-is-not-zero principle this project
    applies everywhere else (AGENTS.md). It must raise, not silently
    resolve to NOT_LISTED."""

    def resolve(hostname: str) -> list[str]:
        raise TimeoutError("dns server did not respond")

    with pytest.raises(SpamhausLookupError):
        check_ip("1.2.3.4", resolve=resolve)


def test_a_resolver_that_raises_spamhaus_lookup_error_directly_is_not_double_wrapped() -> None:
    """If a `resolve` callable already raises `SpamhausLookupError` itself
    (as `default_resolve` does for a non-NXDOMAIN DNS failure), `check_ip`
    must propagate it unchanged rather than wrapping it in a second,
    confusingly nested `SpamhausLookupError`."""

    def resolve(hostname: str) -> list[str]:
        raise SpamhausLookupError("underlying DNS failure")

    with pytest.raises(SpamhausLookupError, match=r"^underlying DNS failure$"):
        check_ip("1.2.3.4", resolve=resolve)


def test_resolver_returning_an_empty_list_is_a_lookup_error_not_not_listed() -> None:
    """A resolver that returns successfully with zero records without
    raising DnsNameNotFoundError is behaving unexpectedly -- treat it as a
    failed check, not as confirmation of a clean IP."""
    resolve = _resolver({f"4.3.2.1.{DEFAULT_ZONE}": []})
    with pytest.raises(SpamhausLookupError):
        check_ip("1.2.3.4", resolve=resolve)


def test_rejects_an_ipv6_address() -> None:
    """The ZEN zone is IPv4-only; this module does not attempt the
    different reverse-nibble encoding IPv6 DNSBL zones require."""
    with pytest.raises(ValueError, match="IPv4"):
        check_ip("2001:db8::1", resolve=_resolver({}))


def test_rejects_a_malformed_ip() -> None:
    with pytest.raises(ValueError, match="IPv4"):
        check_ip("not-an-ip", resolve=_resolver({}))


# --- default_resolve: real DNS mapping, via monkeypatched socket calls ----


def test_default_resolve_returns_addresses_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gethostbyname_ex(hostname: str) -> tuple[str, list[str], list[str]]:
        return (hostname, [], ["127.0.0.2"])

    monkeypatch.setattr(socket, "gethostbyname_ex", fake_gethostbyname_ex)
    assert default_resolve("4.3.2.1.zen.spamhaus.org") == ["127.0.0.2"]


def test_default_resolve_maps_no_such_name_to_dns_name_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gethostbyname_ex(hostname: str) -> tuple[str, list[str], list[str]]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "gethostbyname_ex", fake_gethostbyname_ex)
    with pytest.raises(DnsNameNotFoundError):
        default_resolve("4.3.2.1.zen.spamhaus.org")


def test_default_resolve_maps_other_dns_failures_to_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gethostbyname_ex(hostname: str) -> tuple[str, list[str], list[str]]:
        raise socket.gaierror(-3, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "gethostbyname_ex", fake_gethostbyname_ex)
    with pytest.raises(SpamhausLookupError):
        default_resolve("4.3.2.1.zen.spamhaus.org")
