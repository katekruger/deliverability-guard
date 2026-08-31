"""Beta-binomial posterior on complaint (or bounce) rates, with hierarchical pooling.

This module is the entire statistical argument for the project (BUILD-PLAN.md
§6). The problem it solves:

A sender at 50 emails/day. Gmail's hard ceiling is a 0.3% spam rate. 0.3% of
50 is 0.15 of a message. The only observable outcomes are 0 complaints (0%)
or 1 complaint (2%, 6.7x the limit) -- there is nothing in between. The
metric is quantized above the threshold it measures. A breaker that fires on
a raw point estimate (complaints / sends) crossing 0.3% will fire on that
single complaint, because 2% > 0.3%, and it will be statistically wrong to do
so: n=1 is not enough evidence to distinguish a healthy mailbox from a truly
bad one. That is Smartlead's Bounce Autopause with extra steps.

The fix is to never look at the point estimate. Model the unknown true
complaint rate as a Beta-distributed random variable, update it with observed
counts (a Beta prior is conjugate to a Binomial likelihood, so the update is
closed-form: `Beta(alpha0 + complaints, beta0 + sends - complaints)`), and
trip the breaker on the LOWER BOUND of a one-sided credible interval crossing
the threshold -- i.e. only when the data supports real confidence that the
true rate is that high, not merely that a single unlucky (or lucky) draw
landed there.

STATUS (August 2026, updated): both halves of the earlier status note here
are now fixed. `pooled_prior`/`pooled_posterior` cap the peer group's
effective sample size at `DEFAULT_MAX_POOLED_ESS`, making this genuinely
partial pooling rather than complete pooling weighted by raw volume (ADR
0002's addendum, ENG-4) -- and, separately, `engine.breaker.evaluate` now
calls `pooled_posterior` via its `peer_group` parameter, and
`loops.fast.evaluate_all_mailboxes` -- the chokepoint both `cli.cmd_check`
and `loops.controller.run`'s fast tick share -- builds each mailbox's
same-domain peer group and passes it through, so a real `check`/`run`
actually pools in production rather than only in tests that call this
module directly (CLOSE-1). See ADR 0002 for the full history of both fixes.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from scipy.stats import beta as _beta_dist

from deliverability_guard.engine.state import DataState


@dataclass(frozen=True, slots=True)
class BetaDistribution:
    """A Beta(alpha, beta) distribution -- used as both a prior and a posterior.

    The beta-binomial model is self-conjugate: a posterior is itself just
    another Beta distribution, so updating it further (e.g. updating a
    domain's pooled posterior with one mailbox's own data) uses the exact
    same `update()` as updating a bare prior. There is deliberately no
    separate `Posterior` type.
    """

    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError(
                f"alpha and beta must be positive, got alpha={self.alpha}, beta={self.beta}"
            )

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def lower_bound(self, confidence: float = 0.95) -> float:
        """The value L such that P(true rate >= L) == confidence.

        This is a ONE-SIDED lower confidence bound, not the edge of a
        symmetric interval -- a circuit breaker needs "how bad might this
        genuinely be, with `confidence` worth of certainty," not a
        two-sided estimate. Trip on THIS value crossing a threshold, never
        on `mean`: at n=1 complaint in 50 sends, `mean` alone is close to a
        2% point estimate that a naive breaker would fire on; `lower_bound`
        correctly reflects that there is nowhere near enough evidence to be
        confident of that.
        """
        if not 0 < confidence < 1:
            raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence}")
        # scipy's stubs don't fully type `ppf`'s overloads; the return value
        # is a numpy scalar here (scalar in -> scalar out), and `float()`
        # gives us back a real Python float regardless.
        return float(_beta_dist.ppf(1 - confidence, self.alpha, self.beta))  # pyright: ignore[reportUnknownMemberType]


# Weakly informative: centred near 0.1% (0.5 / (0.5 + 500) ~= 0.0999%), with
# an effective sample size of ~500 -- easily overwhelmed once a mailbox or
# domain has a few thousand real sends.
#
# Why a prior belongs here at all, since a reviewer who hasn't thought about
# this will read it as fudging the numbers: at n=1 complaint in 50 sends,
# there is not enough data to estimate a rate from data alone, full stop.
# Every method has to assume *something* about what's plausible before it's
# seen any data. A flat (uninformative) prior would assume a complaint rate
# uniformly likely to be anywhere from 0% to 100%, which is not a neutral
# assumption for cold outbound email -- it is a wildly implausible one that
# would make the posterior swing on the very first observed complaint.
# Beta(0.5, 500) instead encodes "probably well under 1%, but we're honestly
# not sure yet." It is not a thumb on the scale toward a favorable answer: it
# is trivially overwhelmed by a few hundred real complaints (see
# `test_forty_complaints_in_five_thousand_sends_does_trip`), and it can never
# permanently mask a genuinely bad domain from view.
DEFAULT_PRIOR = BetaDistribution(alpha=0.5, beta=500.0)


def update(prior: BetaDistribution, sends: int, complaints: int) -> BetaDistribution:
    """Beta(alpha0 + complaints, beta0 + sends - complaints).

    `sends == 0` is valid input here and returns the prior unchanged with no
    error -- deciding that zero/absent sends means INSUFFICIENT_DATA is a
    policy choice that belongs to the caller (see `evaluate_mailbox` and
    engine/state.py), not to this function. Nothing in this module ever
    divides by `sends`, so a division-by-zero here is not merely guarded
    against -- it is structurally impossible.
    """
    if sends < 0:
        raise ValueError(f"sends must be >= 0, got {sends}")
    if not 0 <= complaints <= sends:
        raise ValueError(f"complaints must be between 0 and sends ({sends}), got {complaints}")
    return BetaDistribution(alpha=prior.alpha + complaints, beta=prior.beta + sends - complaints)


def breaches(posterior: BetaDistribution, threshold: float, *, confidence: float = 0.95) -> bool:
    """Whether the posterior's lower confidence bound crosses `threshold`."""
    return posterior.lower_bound(confidence) >= threshold


@dataclass(frozen=True, slots=True)
class MailboxEvaluation:
    state: DataState
    posterior: BetaDistribution | None
    breached: bool


def evaluate_mailbox(
    prior: BetaDistribution,
    sends: int,
    complaints: int,
    threshold: float,
    *,
    confidence: float = 0.95,
) -> MailboxEvaluation:
    """The single-mailbox, single-period entry point tying state and posterior together.

    `sends == 0` short-circuits to INSUFFICIENT_DATA with no posterior
    computed -- there is genuinely nothing to say about a rate with zero
    observations, and returning `None` rather than some default posterior
    keeps that honest instead of implying a computed answer exists.
    """
    if sends < 0:
        raise ValueError(f"sends must be >= 0, got {sends}")
    if not 0 <= complaints <= sends:
        raise ValueError(f"complaints must be between 0 and sends ({sends}), got {complaints}")
    if sends == 0:
        return MailboxEvaluation(state=DataState.INSUFFICIENT_DATA, posterior=None, breached=False)
    posterior = update(prior, sends, complaints)
    return MailboxEvaluation(
        state=DataState.OK,
        posterior=posterior,
        breached=breaches(posterior, threshold, confidence=confidence),
    )


@dataclass(frozen=True, slots=True)
class GroupObservation:
    """One member's sends/complaints within a pooling group.

    The same shape represents a mailbox within a domain, or a domain within
    a tenant -- the pooling math is identical at either level (BUILD-PLAN.md
    §6: "partial-pool across mailboxes within a domain, and across domains
    within a tenant"), so this type and the functions below are deliberately
    generic rather than duplicated per level.
    """

    sends: int
    complaints: int
    label: str = ""


# The cap on how much effective sample size (alpha + beta, contributed on
# top of the base prior) a peer group is allowed to inject into an
# individual member's prior in `pooled_prior` below.
#
# This is what makes the pooling PARTIAL rather than COMPLETE. Without a
# cap, `pooled_prior` weights the group by its raw total volume, which grows
# without bound as the group grows -- a peer group large enough eventually
# swamps ANY individual mailbox's own evidence, no matter how much evidence
# that mailbox has of its own. That is complete pooling (the individual
# reduces to the group mean) wearing hierarchical-pooling's name, and it was
# an ENG-4 audit finding (2026-08-30): a mailbox sending at 16x Gmail's
# ceiling, sitting on 99 healthy peers, was read as healthy -- its own
# weight in its own posterior was 1%.
#
# 5,000 is not an arbitrary round number: it is the volume `docs/statistics.md`
# and this module's own tests (`test_forty_complaints_in_five_thousand_sends_does_trip`)
# already treat as "enough of a mailbox's own data to be confident on its
# own" -- a mailbox at or above this many sends of its own evidence is
# guaranteed a group-relative weight no smaller than
# `own_sends / (own_sends + DEFAULT_MAX_POOLED_ESS + base_prior_ess)`,
# regardless of how large the peer group grows. See ADR 0002.
DEFAULT_MAX_POOLED_ESS = 5000.0


def pooled_prior(
    prior: BetaDistribution,
    group: Iterable[GroupObservation],
    *,
    max_ess: float = DEFAULT_MAX_POOLED_ESS,
) -> BetaDistribution:
    """The group-level posterior, formed by aggregating every member's data.

    This becomes the PRIOR for an individual member in `pooled_posterior`
    below. Pass only the OTHER members here (leave-one-out) -- including a
    member's own data would count it twice once `pooled_posterior` adds it
    again on top.

    The group's total effective sample size (its contribution to alpha +
    beta) is capped at `max_ess`: if the group's raw total volume exceeds
    it, both `total_sends` and `total_complaints` are scaled down by the
    same factor before being folded into `prior`, which preserves the
    group's observed rate exactly while bounding how much weight it can
    carry. This is what makes this partial rather than complete pooling --
    see the module-level docstring at `DEFAULT_MAX_POOLED_ESS` for why the
    uncapped version was wrong. The property this must satisfy, and does: a
    mailbox with enough of its own evidence to breach a threshold on that
    evidence alone must keep breaching regardless of how many healthy peers
    exist (`test_monotonicity_more_healthy_peers_never_masks_a_breaching_mailbox`).
    """
    if max_ess <= 0:
        raise ValueError(f"max_ess must be > 0, got {max_ess}")
    observations = list(group)
    total_sends: float = sum(obs.sends for obs in observations)
    total_complaints: float = sum(obs.complaints for obs in observations)
    if total_sends > max_ess:
        scale = max_ess / total_sends
        total_sends *= scale
        total_complaints *= scale
    return BetaDistribution(
        alpha=prior.alpha + total_complaints,
        beta=prior.beta + total_sends - total_complaints,
    )


def pooled_posterior(
    prior: BetaDistribution,
    other_members: Iterable[GroupObservation],
    own_sends: int,
    own_complaints: int,
    *,
    max_ess: float = DEFAULT_MAX_POOLED_ESS,
) -> BetaDistribution:
    """Partial pooling: a member's own data updates its group's posterior.

    This is the mathematically correct answer to "a single mailbox never has
    enough data" (BUILD-PLAN.md §6): a mailbox with 50 sends of its own is
    dominated by its domain's aggregate (a large effective sample size
    contributed by other mailboxes, capped at `max_ess`); a mailbox with
    `max_ess` or more sends of its own is guaranteed a weight in its own
    posterior at least equal to the peer group's -- own evidence is never
    added under a cap, only the group's is, so enough of it always
    outweighs whatever the (bounded) group contributes.

    This is an empirical-Bayes / sequential-conditioning approximation of a
    full multilevel hierarchical model -- it plugs the group's own posterior
    in as a prior rather than jointly estimating a shared hyperprior across
    every group at once (which would need MCMC or variational inference to
    fit). See ADR 0002 for why that tradeoff is the right one here, and what
    it does and doesn't assume.

    STATUS (August 2026, updated): both gaps the earlier version of this
    note described are closed. The `max_ess` cap (ADR 0002's addendum,
    ENG-4) makes this genuinely partial pooling: a mailbox with 5,000 sends
    of its own is now guaranteed a weight in its own posterior at least
    equal to the (bounded) peer group's, however large that group grows.
    And `engine.breaker.evaluate` calls this function whenever its
    `peer_group` parameter is given, with `loops.fast.evaluate_all_mailboxes`
    building and passing each mailbox's real same-domain peer group in
    production (CLOSE-1) -- this is no longer reachable only from tests.
    See ADR 0002 for the full history.
    """
    group_posterior = pooled_prior(prior, other_members, max_ess=max_ess)
    return update(group_posterior, own_sends, own_complaints)
