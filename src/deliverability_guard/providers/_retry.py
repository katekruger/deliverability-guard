"""Shared retry-with-backoff for provider HTTP calls. Internal -- not part of
the public provider driver API; both instantly.py and smartlead.py use this
rather than each implementing their own.
"""

import random
import time
from collections.abc import Callable

import httpx

from deliverability_guard.providers.base import RateLimitExceededError


def request_with_retry(
    request: Callable[[], httpx.Response],
    *,
    max_attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    rand: random.Random | None = None,
) -> httpx.Response:
    """Call `request()`, retrying with exponential backoff and full jitter on 429.

    Neither Instantly's nor Smartlead's rate limits are publicly documented
    (BUILD-PLAN.md §5, §8), so this assumes 429s can happen at any time. Any
    response with another status code is returned as-is, unexamined -- this
    function's only job is to keep a 429 from ever reaching a driver's
    caller looking like "the request failed" or, worse, like a data point.
    A rate limit is not evidence about a mailbox and must never be treated
    as a breach.

    `sleep` and `rand` are injectable so tests can exercise real retry logic
    without a real clock or real randomness.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    rng = rand if rand is not None else random.Random()  # noqa: S311 -- jitter, not cryptographic
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        response = request()
        if response.status_code != 429:
            return response
        if attempt == max_attempts:
            raise RateLimitExceededError(
                f"rate limited after {max_attempts} attempt(s) (last status 429)"
            )
        retry_after = _retry_after_seconds(response)
        wait = retry_after if retry_after is not None else rng.uniform(0, delay)
        sleep(wait)
        delay = min(delay * 2, max_delay)
    raise AssertionError("unreachable: loop always returns or raises")  # pragma: no cover


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
