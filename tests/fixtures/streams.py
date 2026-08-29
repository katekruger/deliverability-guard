"""Synthetic send/complaint streams for exercising the breaker across volume
regimes (BUILD-PLAN.md §6, §10). Test-only -- not part of the shipped package.
"""

import random
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DailyCounts:
    day: int
    sends: int
    complaints: int


def synthetic_stream(
    *,
    daily_sends: int,
    true_complaint_rate: float,
    days: int,
    seed: int,
) -> Iterator[DailyCounts]:
    """Simulate `days` of daily sends at a fixed volume and true complaint rate.

    Each day's complaint count is drawn Binomial(daily_sends,
    true_complaint_rate). Deterministic given `seed`, so tests using this are
    reproducible.
    """
    if daily_sends < 0:
        raise ValueError(f"daily_sends must be >= 0, got {daily_sends}")
    if not 0 <= true_complaint_rate <= 1:
        raise ValueError(f"true_complaint_rate must be in [0, 1], got {true_complaint_rate}")
    rng = random.Random(seed)  # noqa: S311 -- synthetic test data, not cryptographic
    for day in range(days):
        complaints = rng.binomialvariate(daily_sends, true_complaint_rate) if daily_sends else 0
        yield DailyCounts(day=day, sends=daily_sends, complaints=complaints)
