"""Subdomain segregation advisor.

Postmaster aggregates by DOMAIN (BUILD-PLAN.md §9) -- it does not offer a
generic "break this domain's stats down by campaign" view. `Feedback-ID`
(feedback_id.py) gets campaign-level attribution specifically for
`FEEDBACK_LOOP_SPAM_RATE`, but not for the other metrics this project reads
from Postmaster (`AUTH_SUCCESS_RATE`, `DELIVERY_ERROR_RATE`, and so on).
Sending each distinct campaign CLASS from its own subdomain makes
Postmaster's own per-domain aggregation double as per-campaign-class
attribution for every metric it reports, not only the ones Feedback-ID
covers.

BE HONEST ABOUT WHAT THIS IS: this module recommends a sending
architecture. It cannot make anyone actually send from separate
subdomains, and it cannot verify that they are, beyond checking whether a
domain string matches what was recommended for a given class -- that check
only catches "you told this tool you were using the recommended subdomain
and you weren't," not "you're sending from the recommended subdomain in
reality." The recommendation is real, and following it is genuinely the
fix for the attribution problem in docs/limits.md -- but this is a naming-
scheme generator and a self-reported consistency check, not automation of
the underlying operational change. Do not present it as more than that.
"""

import re
from dataclasses import dataclass

# DNS labels: letters, digits, hyphens; must not start or end with a
# hyphen. This is deliberately conservative relative to the full DNS label
# grammar (which technically permits more) because a subdomain here also
# has to survive being embedded in a From: address and Postmaster's own
# domain-string matching, not just bare DNS resolution.
_DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_MAX_LABEL_LENGTH = 63


@dataclass(frozen=True, slots=True)
class SubdomainRecommendation:
    campaign_class: str
    recommended_subdomain: str
    rationale: str


def recommend_subdomain(*, root_domain: str, campaign_class: str) -> SubdomainRecommendation:
    if not root_domain:
        raise ValueError("root_domain must not be empty")
    label = _slugify(campaign_class)
    subdomain = f"{label}.{root_domain}"
    return SubdomainRecommendation(
        campaign_class=campaign_class,
        recommended_subdomain=subdomain,
        rationale=(
            f"Postmaster aggregates by domain, not by campaign. Sending "
            f"{campaign_class!r} exclusively from {subdomain} makes "
            f"Postmaster's own per-domain stats for that subdomain become "
            f"per-campaign-class attribution for every metric it reports -- "
            f"but ONLY if every message in this class is actually sent from "
            f"there. This tool cannot enforce that; it can only tell you "
            f"the scheme and check what you report back against it."
        ),
    )


def matches_recommendation(actual_domain: str, recommendation: SubdomainRecommendation) -> bool:
    """A self-reported consistency check, not verification: this compares
    whatever domain string the caller supplies against the recommendation.
    It cannot independently confirm that domain is where mail was really
    sent from."""
    return actual_domain.strip().lower() == recommendation.recommended_subdomain.lower()


def _slugify(campaign_class: str) -> str:
    if not campaign_class:
        raise ValueError("campaign_class must not be empty")
    lowered = campaign_class.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise ValueError(
            f"campaign_class {campaign_class!r} has no characters usable in a DNS label"
        )
    if len(slug) > _MAX_LABEL_LENGTH:
        raise ValueError(
            f"campaign_class {campaign_class!r} produces a label longer than "
            f"{_MAX_LABEL_LENGTH} characters: {slug!r}"
        )
    if not _DNS_LABEL.fullmatch(slug):  # pragma: no cover
        # Defensive: the substitution above already collapses every run of
        # non-alnum characters to a single hyphen and strips leading/
        # trailing hyphens, so `slug` should always satisfy `_DNS_LABEL` by
        # construction. Kept as a hard check rather than an assertion so a
        # future change to the substitution logic fails loudly instead of
        # silently producing an invalid subdomain recommendation.
        raise ValueError(
            f"campaign_class {campaign_class!r} produces an invalid DNS label: {slug!r}"
        )
    return slug
