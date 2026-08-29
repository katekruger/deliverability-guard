"""Tests for providers/_retry.py: exponential backoff with jitter on 429.

429 mid-evaluation -> back off; do NOT treat as a breach. These tests never
sleep for real -- `sleep` is injected and its calls are recorded instead.
"""

import random

import httpx
import pytest

from deliverability_guard.providers._retry import request_with_retry
from deliverability_guard.providers.base import RateLimitExceededError
from fixtures.http import constant_sleep_recorder


def _response(status_code: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code, headers=headers or {})


def test_returns_immediately_on_success() -> None:
    calls = iter([_response(200)])
    sleep, waited = constant_sleep_recorder()
    result = request_with_retry(lambda: next(calls), sleep=sleep)
    assert result.status_code == 200
    assert waited == []


def test_retries_past_a_429_then_succeeds() -> None:
    calls = iter([_response(429), _response(429), _response(200)])
    sleep, waited = constant_sleep_recorder()
    result = request_with_retry(
        lambda: next(calls), sleep=sleep, rand=random.Random(0), max_attempts=5
    )
    assert result.status_code == 200
    assert len(waited) == 2


def test_raises_rate_limit_exceeded_after_exhausting_attempts() -> None:
    calls = iter([_response(429)] * 10)
    sleep, _ = constant_sleep_recorder()
    with pytest.raises(RateLimitExceededError):
        request_with_retry(lambda: next(calls), sleep=sleep, max_attempts=3)


def test_does_not_treat_a_429_as_success_it_keeps_retrying() -> None:
    """The caller never sees a 429 response object at all when retries are
    available -- it's fully absorbed by this function, never mistakable for
    a data point."""
    calls = iter([_response(429), _response(200)])
    sleep, waited = constant_sleep_recorder()
    result = request_with_retry(lambda: next(calls), sleep=sleep, max_attempts=5)
    assert result.status_code == 200
    assert len(waited) == 1


def test_honors_retry_after_header_when_present() -> None:
    calls = iter([_response(429, headers={"Retry-After": "2.5"}), _response(200)])
    sleep, waited = constant_sleep_recorder()
    request_with_retry(lambda: next(calls), sleep=sleep, max_attempts=5)
    assert waited == [2.5]


def test_ignores_unparseable_retry_after_header_and_falls_back_to_jitter() -> None:
    calls = iter([_response(429, headers={"Retry-After": "not-a-number"}), _response(200)])
    sleep, waited = constant_sleep_recorder()
    request_with_retry(
        lambda: next(calls), sleep=sleep, rand=random.Random(0), max_attempts=5, base_delay=1.0
    )
    assert len(waited) == 1
    assert 0.0 <= waited[0] <= 1.0


def test_backoff_delay_grows_and_is_capped_at_max_delay() -> None:
    calls = iter([_response(429)] * 6 + [_response(200)])
    sleep, waited = constant_sleep_recorder()
    # rand.uniform(0, delay) with a fixed seed lets us check the ceiling grew.
    request_with_retry(
        lambda: next(calls),
        sleep=sleep,
        rand=random.Random(1),
        max_attempts=7,
        base_delay=1.0,
        max_delay=4.0,
    )
    assert len(waited) == 6
    # Every wait must respect the (growing, then capped) delay ceiling.
    ceilings = [1.0, 2.0, 4.0, 4.0, 4.0, 4.0]
    for wait, ceiling in zip(waited, ceilings, strict=True):
        assert 0.0 <= wait <= ceiling


def test_rejects_max_attempts_below_one() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        request_with_retry(lambda: _response(200), max_attempts=0)
