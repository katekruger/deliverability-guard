"""Spamhaus DQS (Data Query Service) DNSBL lookups (BUILD-PLAN.md §4 item
#25): "the only free programmatic blocklist worth having" -- Talos,
SenderScore, and Validity have no public API (BUILD-PLAN.md §8).

A DNSBL lookup is a reversed-octet DNS query under a well-known zone: to
check `1.2.3.4` against the ZEN zone (Spamhaus's combined SBL+XBL+PBL
list), query `4.3.2.1.zen.spamhaus.org`. A confirmed NXDOMAIN means "not
listed." An A record answer means "listed," and *which* address in the
127.0.0.x range identifies which sub-list matched.

The one thing this module refuses to get wrong: a genuine lookup failure
(DNS timeout, SERVFAIL, network error) is NOT evidence of a clean IP. A
resolver's answer for "confirmed not listed" (`DnsNameNotFoundError`, i.e.
NXDOMAIN) and every other failure mode are kept structurally distinct --
`check_ip` raises `SpamhausLookupError` for the latter rather than quietly
reporting `NOT_LISTED`, the same missing-data-is-not-zero principle
AGENTS.md applies everywhere else in this project. The Postmaster privacy-
threshold landmine (`engine/state.py`) and this one are the same shape: an
absent answer is not automatically a good answer.

This module is IPv4-only. Spamhaus's IPv6 DNSBL zones use a different
reverse-nibble encoding this module does not implement; `check_ip` rejects
a non-IPv4 address rather than silently building a wrong query for it.
"""

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

DEFAULT_ZONE = "zen.spamhaus.org"


class DnsNameNotFoundError(Exception):
    """The resolver's answer for a confirmed NXDOMAIN -- "this name does
    not exist," which for a DNSBL query means "not listed." This is the
    ONLY exception `check_ip` treats as a clean result; every other
    exception a resolver raises is a failed check, not evidence of a clean
    IP.
    """


class SpamhausLookupError(Exception):
    """The lookup itself failed (timeout, SERVFAIL, network error, or a
    resolver that returned an empty answer without raising
    `DnsNameNotFoundError`) -- distinct from a confirmed "not listed." See
    the module docstring."""


class SpamhausListing(Enum):
    NOT_LISTED = auto()
    SBL = auto()  # 127.0.0.2 -- Spamhaus Block List: direct spam source
    CSS = auto()  # 127.0.0.3 -- CSS: compromised/snowshoe spam source
    XBL = auto()  # 127.0.0.4-7 -- exploited/infected (open proxy, malware)
    PBL_ISP = auto()  # 127.0.0.10 -- ISP-policy dynamic/residential range
    PBL_SPAMHAUS = auto()  # 127.0.0.11 -- Spamhaus-maintained policy range
    UNKNOWN_LISTED = auto()
    """Listed under a return code this module doesn't recognize (e.g.
    Spamhaus adds a new sub-list). Never coerced to NOT_LISTED -- an
    unrecognized code is still a listing, just one this module can't name
    yet."""


_RETURN_CODES: dict[str, SpamhausListing] = {
    "127.0.0.2": SpamhausListing.SBL,
    "127.0.0.3": SpamhausListing.CSS,
    "127.0.0.4": SpamhausListing.XBL,
    "127.0.0.5": SpamhausListing.XBL,
    "127.0.0.6": SpamhausListing.XBL,
    "127.0.0.7": SpamhausListing.XBL,
    "127.0.0.10": SpamhausListing.PBL_ISP,
    "127.0.0.11": SpamhausListing.PBL_SPAMHAUS,
}


@dataclass(frozen=True, slots=True)
class SpamhausResult:
    ip: str
    query: str
    listing: SpamhausListing
    raw_codes: tuple[str, ...]
    """Every return code the resolver reported, verbatim, in the order
    returned -- `listing` is derived from the first, but the full set is
    kept for audit rather than discarded."""


def check_ip(
    ip: str,
    *,
    zone: str = DEFAULT_ZONE,
    resolve: Callable[[str], list[str]],
) -> SpamhausResult:
    """Look up `ip` against `zone`. Raises `ValueError` for a non-IPv4
    address, `SpamhausLookupError` for anything that isn't a confirmed
    NXDOMAIN or a valid listing answer.

    `resolve` has no default and must be supplied explicitly -- pass
    `default_resolve` for a real DNS lookup, or a fake resolver in tests
    (AGENTS.md: no live API calls in tests, which applies to DNS the same
    as HTTP). Requiring it explicitly means a test can never accidentally
    fall through to a real lookup by omitting the argument.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError(f"{ip!r} is not a valid IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError(f"{ip!r} is not an IPv4 address; the {zone} zone is IPv4-only")

    query = f"{_reversed_octets(str(address))}.{zone}"
    try:
        codes = resolve(query)
    except DnsNameNotFoundError:
        return SpamhausResult(ip=ip, query=query, listing=SpamhausListing.NOT_LISTED, raw_codes=())
    except Exception as exc:
        if isinstance(exc, SpamhausLookupError):
            raise
        raise SpamhausLookupError(f"lookup for {query} failed: {exc}") from exc

    if not codes:
        raise SpamhausLookupError(
            f"resolver for {query} returned no records without raising DnsNameNotFoundError"
        )
    listing = _RETURN_CODES.get(codes[0], SpamhausListing.UNKNOWN_LISTED)
    return SpamhausResult(ip=ip, query=query, listing=listing, raw_codes=tuple(codes))


def _reversed_octets(ipv4: str) -> str:
    return ".".join(reversed(ipv4.split(".")))


def default_resolve(hostname: str) -> list[str]:
    """The real DNS implementation, used when `check_ip` isn't given a
    `resolve` override. Never called in tests -- every test injects a fake
    resolver instead (AGENTS.md: no live API calls in tests, which applies
    to DNS the same as HTTP).

    Maps a confirmed "no such name" answer to `DnsNameNotFoundError`;
    anything else (timeout, SERVFAIL, an unexpected error) is wrapped in
    `SpamhausLookupError` rather than allowed to look like "not listed."
    """
    try:
        _, _, addresses = socket.gethostbyname_ex(hostname)
    except socket.gaierror as exc:
        not_found_codes = {
            code
            for code in (getattr(socket, "EAI_NONAME", None), getattr(socket, "EAI_NODATA", None))
            if code is not None
        }
        if exc.errno in not_found_codes:
            raise DnsNameNotFoundError(hostname) from exc
        raise SpamhausLookupError(f"DNS lookup for {hostname} failed: {exc}") from exc
    return addresses
