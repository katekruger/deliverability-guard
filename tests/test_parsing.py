"""Tests for providers/_parsing.py: typed JSON extraction and UTC normalization."""

from datetime import date

import pytest

from deliverability_guard.providers._parsing import (
    normalize_to_utc_date,
    require_dict,
    require_int,
    require_list,
    require_str,
)
from deliverability_guard.providers.base import MalformedResponseError


def test_require_dict_accepts_a_dict() -> None:
    assert require_dict({"a": 1}, "x") == {"a": 1}


def test_require_dict_rejects_non_dict() -> None:
    with pytest.raises(MalformedResponseError, match="object"):
        require_dict([1, 2, 3], "x")


def test_require_list_accepts_a_list() -> None:
    assert require_list([1, 2], "x") == [1, 2]


def test_require_list_rejects_non_list() -> None:
    with pytest.raises(MalformedResponseError, match="list"):
        require_list({"a": 1}, "x")


def test_require_str_accepts_a_string() -> None:
    assert require_str({"k": "v"}, "k", "x") == "v"


def test_require_str_rejects_missing_key() -> None:
    with pytest.raises(MalformedResponseError, match="'k'"):
        require_str({}, "k", "x")


def test_require_str_rejects_wrong_type() -> None:
    with pytest.raises(MalformedResponseError, match="'k'"):
        require_str({"k": 5}, "k", "x")


def test_require_int_accepts_an_int() -> None:
    assert require_int({"k": 5}, "k", "x") == 5


def test_require_int_rejects_a_bool() -> None:
    """bool is a subclass of int in Python -- a provider sending `true` for
    a count field must be treated as malformed, not silently coerced to 1."""
    with pytest.raises(MalformedResponseError, match="'k'"):
        require_int({"k": True}, "k", "x")


def test_require_int_rejects_a_float() -> None:
    with pytest.raises(MalformedResponseError, match="'k'"):
        require_int({"k": 5.5}, "k", "x")


def test_normalize_bare_date_returned_as_is() -> None:
    assert normalize_to_utc_date("2026-08-01", "x") == date(2026, 8, 1)


def test_normalize_timestamp_with_utc_offset_converts_correctly() -> None:
    """The off-by-one-day case: 23:30 in UTC-7 is already the next day in UTC."""
    assert normalize_to_utc_date("2026-08-01T23:30:00-07:00", "x") == date(2026, 8, 2)


def test_normalize_timestamp_already_utc_is_unchanged() -> None:
    assert normalize_to_utc_date("2026-08-01T12:00:00+00:00", "x") == date(2026, 8, 1)


def test_normalize_rejects_unparseable_date() -> None:
    with pytest.raises(MalformedResponseError, match="x"):
        normalize_to_utc_date("not-a-date", "x")
