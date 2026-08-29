"""Shared JSON-response parsing helpers. Internal -- not part of the public
provider driver API.

httpx deserializes JSON into plain `object`-typed values, so every field
pulled from a provider response needs an explicit type check before use --
under pyright strict, and honestly under any correctness standard, a
provider is an untrusted boundary and "the field we expected" is not the
same claim as "the field that's actually there."
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import cast

from deliverability_guard.providers.base import MalformedResponseError


def require_dict(value: object, provider: str, what: str = "response body") -> dict[str, object]:
    """Narrow an arbitrary JSON-decoded value to a string-keyed object.

    JSON object keys are always strings, so once `isinstance(value, dict)`
    holds, treating it as `dict[str, object]` is safe -- the cast just gives
    pyright the concrete type it can't infer from a bare `dict` isinstance
    check on its own.
    """
    if not isinstance(value, dict):
        raise MalformedResponseError(
            f"{provider}: expected {what} to be an object, got {type(value).__name__}"
        )
    return cast(dict[str, object], value)


def require_list(value: object, provider: str, what: str = "response body") -> list[object]:
    if not isinstance(value, list):
        raise MalformedResponseError(
            f"{provider}: expected {what} to be a list, got {type(value).__name__}"
        )
    return cast(list[object], value)


def require_str(row: Mapping[str, object], key: str, provider: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise MalformedResponseError(
            f"{provider}: expected '{key}' to be a string, got {type(value).__name__}"
        )
    return value


def require_int(row: Mapping[str, object], key: str, provider: str) -> int:
    value = row.get(key)
    # bool is a subclass of int in Python; a provider sending `true`/`false`
    # for a count field is malformed, not a valid 0/1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedResponseError(
            f"{provider}: expected '{key}' to be an integer, got {type(value).__name__}"
        )
    return value


def normalize_to_utc_date(raw: str, provider: str) -> date:
    """Parse an ISO 8601 date or datetime string and return its UTC calendar date.

    A bare date with no time or offset (e.g. "2026-08-01") is returned as-is
    -- there is no timezone information to convert FROM in that case, and
    treating it as anything other than already-UTC would be inventing
    precision the input doesn't have. A full timestamp with an explicit UTC
    offset is properly converted, which is where the real off-by-one-day risk
    lives: a provider reporting "2026-08-01T23:30:00-07:00" is reporting
    2026-08-02 in UTC, not 2026-08-01.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise MalformedResponseError(f"{provider}: could not parse date '{raw}'") from exc
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(UTC).date()
