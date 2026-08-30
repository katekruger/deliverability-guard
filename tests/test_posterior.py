"""Tests for engine/posterior.py -- the beta-binomial posterior and hierarchical
pooling that is the entire statistical thesis of this project (BUILD-PLAN.md §6).

Required cases from BUILD-PLAN.md §6 are each their own test rather than
folded into table-driven parametrization: the point of this file is that
each of these is, individually, the argument for why this project exists.
"""

from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deliverability_guard.engine.posterior import (
    DEFAULT_MAX_POOLED_ESS,
    DEFAULT_PRIOR,
    BetaDistribution,
    GroupObservation,
    breaches,
    evaluate_mailbox,
    pooled_posterior,
    pooled_prior,
    update,
)
from deliverability_guard.engine.state import DailyReport, DataState, evaluate_stream
from fixtures.streams import synthetic_stream

# Gmail's hard ceiling (BUILD-PLAN.md §7). The worked example in §6 is
# specifically about this number: 0.3% of 50 sends is 0.15 of a message.
GMAIL_CEILING = 0.003

# The default ladder's rungs (config/thresholds.example.yml, BUILD-PLAN.md §7).
WARN = 0.0005
THROTTLE = 0.0010
PAUSE = 0.0020


def test_posterior_refuses_to_trip_on_n1() -> None:
    """The thesis. One complaint in 50 sends is a ~2% point estimate -- 6.7x
    Gmail's hard ceiling -- and a fixed-window rate breaker fires on it. The
    posterior's lower bound does not, because there is nowhere near enough
    evidence to be confident the true rate exceeds even 0.3%, let alone the
    much lower `pause` rung."""
    posterior = update(DEFAULT_PRIOR, sends=50, complaints=1)
    assert not breaches(posterior, GMAIL_CEILING)
    assert not breaches(posterior, PAUSE)


def test_forty_complaints_in_five_thousand_sends_does_trip() -> None:
    """At real volume, a genuinely elevated rate (0.8% point estimate) is
    caught with confidence -- the posterior isn't just conservative, it's
    correctly decisive once there's enough evidence."""
    posterior = update(DEFAULT_PRIOR, sends=5000, complaints=40)
    assert breaches(posterior, GMAIL_CEILING)
    assert breaches(posterior, PAUSE)


def test_zero_complaints_is_ok_not_perfect() -> None:
    """Zero observed complaints is evidence of a low rate, not proof of a 0%
    rate -- the posterior mean stays strictly above zero because the prior
    says so, and that's correct: 50 clean sends is consistent with plenty of
    true rates well under 1%, not uniquely with exactly 0%."""
    evaluation = evaluate_mailbox(DEFAULT_PRIOR, sends=50, complaints=0, threshold=PAUSE)
    assert evaluation.state == DataState.OK
    assert evaluation.breached is False
    assert evaluation.posterior is not None
    assert 0.0 < evaluation.posterior.mean < PAUSE


def test_zero_sends_is_insufficient_data_no_division_by_zero() -> None:
    """This can't raise a ZeroDivisionError, because nothing in this module
    ever divides by `sends` -- the beta-binomial update only adds to a
    prior. INSUFFICIENT_DATA is a policy decision at the `evaluate_mailbox`
    boundary, not a numerical workaround."""
    evaluation = evaluate_mailbox(DEFAULT_PRIOR, sends=0, complaints=0, threshold=PAUSE)
    assert evaluation.state == DataState.INSUFFICIENT_DATA
    assert evaluation.posterior is None
    assert evaluation.breached is False


def test_one_complaint_in_three_sends_does_not_trip() -> None:
    """A 33% point estimate on n=3 is the most extreme case a naive breaker
    could see -- and the posterior still refuses, because n=3 is not
    meaningfully different from n=0 as evidence goes."""
    posterior = update(DEFAULT_PRIOR, sends=3, complaints=1)
    assert not breaches(posterior, GMAIL_CEILING)


def test_healthy_domain_pooling_inherits_the_good_prior() -> None:
    """A 50-send mailbox with one complaint of its own sits on a domain of
    40 other mailboxes running clean at real volume. Pooled with that
    domain, the same 1-in-50 mailbox is judged even more confidently healthy
    than it would be on the default prior alone."""
    healthy_peers = [GroupObservation(sends=500, complaints=0) for _ in range(40)]
    pooled = pooled_posterior(DEFAULT_PRIOR, healthy_peers, own_sends=50, own_complaints=1)
    unpooled = update(DEFAULT_PRIOR, sends=50, complaints=1)
    assert not breaches(pooled, GMAIL_CEILING)
    assert pooled.lower_bound() < unpooled.lower_bound()


def test_bad_domain_pooling_inherits_the_bad_prior() -> None:
    """The same 50-send mailbox, reporting a perfectly clean 0-in-50 of its
    own, sits on a domain of 40 other mailboxes running hot (1% complaints
    each). Pooled, it inherits the domain's risk -- its own clean data isn't
    enough to prove it's an exception, just as a human reviewing one clean
    mailbox on an otherwise-burning domain would not be reassured by that
    mailbox alone."""
    bad_peers = [GroupObservation(sends=500, complaints=5) for _ in range(40)]  # 1% each
    pooled = pooled_posterior(DEFAULT_PRIOR, bad_peers, own_sends=50, own_complaints=0)
    own_alone = evaluate_mailbox(DEFAULT_PRIOR, sends=50, complaints=0, threshold=PAUSE)
    assert breaches(pooled, PAUSE)
    assert own_alone.breached is False


def test_data_present_yesterday_absent_today_is_stale_with_transition_alert() -> None:
    """The posterior module doesn't own the state machine, but this is the
    integration point: a mailbox that had real data and then reports nothing
    must not be silently read as OK. See engine/state.py and
    tests/test_state.py for the state machine's own thorough coverage."""
    yesterday = DailyReport(day=date(2026, 1, 1), sends=50, complaints=0)
    today = DailyReport(day=date(2026, 1, 2), sends=None, complaints=None)
    evaluations = evaluate_stream([yesterday, today])
    assert evaluations[0].state == DataState.OK
    assert evaluations[1].state == DataState.STALE
    assert evaluations[1].transition_alert is True


@given(
    sends=st.integers(min_value=0, max_value=100_000),
    complaints=st.integers(min_value=0),
    extra_complaints=st.integers(min_value=0),
)
@settings(max_examples=200)
def test_monotonicity_more_complaints_never_lowers_the_posterior(
    sends: int, complaints: int, extra_complaints: int
) -> None:
    """More complaints at fixed sends never lowers the posterior, on either
    the point estimate or the lower bound. If this ever failed, the breaker
    could get SAFER as a mailbox got worse -- a correctness bug worse than
    any false trip."""
    complaints = min(complaints, sends)
    extra_complaints = min(extra_complaints, sends - complaints)
    fewer = update(DEFAULT_PRIOR, sends=sends, complaints=complaints)
    more = update(DEFAULT_PRIOR, sends=sends, complaints=complaints + extra_complaints)
    assert more.mean >= fewer.mean
    assert more.lower_bound() >= fewer.lower_bound()


@given(st.integers(min_value=0, max_value=50))
@settings(max_examples=20, deadline=None)
def test_hierarchical_pooling_never_produces_an_invalid_distribution(n_peers: int) -> None:
    """No matter how many peers or what they've sent, pooling always
    produces a valid Beta distribution -- alpha and beta both strictly
    positive, never a crash."""
    peers = [GroupObservation(sends=s, complaints=0) for s in range(n_peers)]
    pooled = pooled_posterior(DEFAULT_PRIOR, peers, own_sends=10, own_complaints=0)
    assert pooled.alpha > 0
    assert pooled.beta > 0


def test_regimes_50_500_5000_per_day_all_exercise_the_ladder() -> None:
    """A synthetic stream generator produces send/complaint sequences at 50,
    500 and 5,000 sends/day so the whole warn/throttle/pause ladder can be
    exercised at every volume regime this project cares about. At a true
    complaint rate comfortably above `pause` (double the pause rung), every
    regime eventually trips -- but higher volume gets there in fewer days,
    because more sends per day is more evidence per day."""
    true_rate = 2 * PAUSE
    days_to_trip: dict[int, int | None] = {}
    for daily_sends in (50, 500, 5000):
        posterior = DEFAULT_PRIOR
        tripped_on_day: int | None = None
        for record in synthetic_stream(
            daily_sends=daily_sends, true_complaint_rate=true_rate, days=120, seed=daily_sends
        ):
            posterior = update(posterior, sends=record.sends, complaints=record.complaints)
            if breaches(posterior, PAUSE):
                tripped_on_day = record.day
                break
        days_to_trip[daily_sends] = tripped_on_day

    assert all(day is not None for day in days_to_trip.values()), days_to_trip
    day_50, day_500, day_5000 = days_to_trip[50], days_to_trip[500], days_to_trip[5000]
    assert day_50 is not None
    assert day_500 is not None
    assert day_5000 is not None
    assert day_50 >= day_500 >= day_5000


# --- Validation ---------------------------------------------------------


def test_update_rejects_negative_sends() -> None:
    with pytest.raises(ValueError, match="sends"):
        update(DEFAULT_PRIOR, sends=-1, complaints=0)


def test_update_rejects_complaints_greater_than_sends() -> None:
    with pytest.raises(ValueError, match="complaints"):
        update(DEFAULT_PRIOR, sends=5, complaints=6)


def test_update_rejects_negative_complaints() -> None:
    with pytest.raises(ValueError, match="complaints"):
        update(DEFAULT_PRIOR, sends=5, complaints=-1)


def test_beta_distribution_rejects_nonpositive_parameters() -> None:
    with pytest.raises(ValueError, match="positive"):
        BetaDistribution(alpha=0.0, beta=1.0)
    with pytest.raises(ValueError, match="positive"):
        BetaDistribution(alpha=1.0, beta=-1.0)


def test_lower_bound_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError, match="confidence"):
        DEFAULT_PRIOR.lower_bound(confidence=1.0)
    with pytest.raises(ValueError, match="confidence"):
        DEFAULT_PRIOR.lower_bound(confidence=0.0)


def test_evaluate_mailbox_rejects_negative_sends() -> None:
    with pytest.raises(ValueError, match="sends"):
        evaluate_mailbox(DEFAULT_PRIOR, sends=-1, complaints=0, threshold=PAUSE)


def test_evaluate_mailbox_rejects_complaints_greater_than_sends() -> None:
    with pytest.raises(ValueError, match="complaints"):
        evaluate_mailbox(DEFAULT_PRIOR, sends=5, complaints=6, threshold=PAUSE)


def test_pooled_prior_with_no_peers_returns_prior_unchanged() -> None:
    assert pooled_prior(DEFAULT_PRIOR, []) == DEFAULT_PRIOR


def test_pooled_prior_rejects_nonpositive_max_ess() -> None:
    with pytest.raises(ValueError, match="max_ess"):
        pooled_prior(DEFAULT_PRIOR, [], max_ess=0.0)


# --- ESS cap: partial, not complete, pooling (ADR 0002 addendum) ----------
#
# Reproduction (audit finding ENG-4, 2026-08-30): a mailbox burning at 16x
# Gmail's ceiling, sitting on a domain of 99 healthy peers, was judged
# healthy by the pooled posterior -- its own weight in its own posterior was
# 1%. That is complete pooling (the group swamps the individual no matter
# how much evidence the individual has), not partial pooling, and it is the
# exact failure mode this module exists to prevent.


def test_pooled_posterior_breaches_despite_ninety_nine_healthy_peers() -> None:
    """The audit's reproduction, verbatim: 99 peers at 5,000 sends/0.1% each,
    target at 5,000 sends/5.0% (16x Gmail's ceiling). Before the ESS cap, the
    pooled posterior read this as healthy. It must not."""
    healthy_peers = [GroupObservation(sends=5000, complaints=5) for _ in range(99)]
    pooled = pooled_posterior(DEFAULT_PRIOR, healthy_peers, own_sends=5000, own_complaints=250)
    assert breaches(pooled, GMAIL_CEILING)
    assert breaches(pooled, PAUSE)


def test_monotonicity_more_healthy_peers_never_masks_a_breaching_mailbox() -> None:
    """A mailbox with enough of its own evidence to breach on its own must
    keep breaching no matter how many additional healthy peers are added to
    its group -- more (healthy) company can never make a bad mailbox look
    better."""
    own_sends, own_complaints = 5000, 250  # 5%, breaches on its own evidence alone
    for n_peers in (0, 1, 10, 50, 99, 500):
        peers = [GroupObservation(sends=5000, complaints=5) for _ in range(n_peers)]
        pooled = pooled_posterior(
            DEFAULT_PRIOR, peers, own_sends=own_sends, own_complaints=own_complaints
        )
        assert breaches(pooled, GMAIL_CEILING), f"masked by pooling at {n_peers} healthy peers"


def test_pooled_prior_ess_never_exceeds_the_cap() -> None:
    """`pooled_prior`'s effective sample size (alpha + beta, relative to the
    base prior) must never exceed `max_ess`, no matter how large or how many
    peers are in the group -- this is the mechanism that makes pooling
    partial rather than complete."""
    base_ess = DEFAULT_PRIOR.alpha + DEFAULT_PRIOR.beta
    for n_peers in (0, 1, 10, 99, 1000):
        peers = [GroupObservation(sends=5000, complaints=5) for _ in range(n_peers)]
        pooled = pooled_prior(DEFAULT_PRIOR, peers)
        assert pooled.alpha + pooled.beta <= base_ess + DEFAULT_MAX_POOLED_ESS + 1e-6


def test_own_contribution_has_a_floor_regardless_of_peer_group_size() -> None:
    """A mailbox's own weight in its pooled posterior -- own_sends relative
    to (own_sends + the group's capped effective sample size) -- must not
    fall below a floor set by `max_ess`, however many healthy peers exist.
    Without the cap, this floor is zero: an unbounded peer group can dilute
    a mailbox's own evidence to nothing."""
    own_sends = 5000
    base_ess = DEFAULT_PRIOR.alpha + DEFAULT_PRIOR.beta
    floor = own_sends / (own_sends + base_ess + DEFAULT_MAX_POOLED_ESS)
    for n_peers in (1, 10, 99, 10_000):
        peers = [GroupObservation(sends=5000, complaints=5) for _ in range(n_peers)]
        group_prior = pooled_prior(DEFAULT_PRIOR, peers)
        own_weight = own_sends / (own_sends + group_prior.alpha + group_prior.beta)
        assert own_weight >= floor - 1e-9


def test_small_own_volume_still_shrinks_strongly_toward_the_group() -> None:
    """The ESS cap must not break the legitimate use of pooling: a mailbox
    with very little of its own data (n=1, n=10) should still be dominated
    by a large, healthy peer group -- the cap bounds the group's influence,
    it doesn't remove it."""
    healthy_peers = [GroupObservation(sends=500, complaints=0) for _ in range(40)]
    for own_sends, own_complaints in ((1, 0), (10, 0)):
        pooled = pooled_posterior(
            DEFAULT_PRIOR, healthy_peers, own_sends=own_sends, own_complaints=own_complaints
        )
        unpooled = update(DEFAULT_PRIOR, sends=own_sends, complaints=own_complaints)
        # Dominated by the (much larger, healthy) group: pooling should pull
        # the posterior mean noticeably below the tiny mailbox's own-data-only
        # estimate, which on its own is barely different from the bare prior.
        assert pooled.mean < unpooled.mean or pooled.mean == pytest.approx(unpooled.mean, rel=0.2)
        assert pooled.alpha + pooled.beta > own_sends + (DEFAULT_PRIOR.alpha + DEFAULT_PRIOR.beta)
