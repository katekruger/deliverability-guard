"""Tests for identity/subdomain_advisor.py."""

import pytest

from deliverability_guard.identity.subdomain_advisor import (
    matches_recommendation,
    recommend_subdomain,
)


def test_recommend_subdomain_slugifies_the_campaign_class() -> None:
    rec = recommend_subdomain(root_domain="example.com", campaign_class="Cold Outbound Q3")
    assert rec.recommended_subdomain == "cold-outbound-q3.example.com"
    assert rec.campaign_class == "Cold Outbound Q3"


def test_recommend_subdomain_rationale_names_the_subdomain() -> None:
    rec = recommend_subdomain(root_domain="example.com", campaign_class="warmup")
    assert rec.recommended_subdomain in rec.rationale


def test_recommend_subdomain_rejects_empty_root_domain() -> None:
    with pytest.raises(ValueError, match="root_domain"):
        recommend_subdomain(root_domain="", campaign_class="warmup")


def test_recommend_subdomain_rejects_a_campaign_class_with_no_usable_characters() -> None:
    with pytest.raises(ValueError, match="no characters"):
        recommend_subdomain(root_domain="example.com", campaign_class="!!!")


def test_recommend_subdomain_rejects_an_overlong_label() -> None:
    with pytest.raises(ValueError, match="longer than"):
        recommend_subdomain(root_domain="example.com", campaign_class="a" * 64)


def test_recommend_subdomain_strips_leading_and_trailing_separators() -> None:
    rec = recommend_subdomain(root_domain="example.com", campaign_class="-warmup-")
    assert rec.recommended_subdomain == "warmup.example.com"


def test_recommend_subdomain_empty_campaign_class_raises() -> None:
    with pytest.raises(ValueError, match="campaign_class"):
        recommend_subdomain(root_domain="example.com", campaign_class="")


# --- matches_recommendation --------------------------------------------


def test_matches_recommendation_true_for_the_exact_recommended_subdomain() -> None:
    rec = recommend_subdomain(root_domain="example.com", campaign_class="warmup")
    assert matches_recommendation("warmup.example.com", rec) is True


def test_matches_recommendation_is_case_insensitive() -> None:
    rec = recommend_subdomain(root_domain="example.com", campaign_class="warmup")
    assert matches_recommendation("WARMUP.EXAMPLE.COM", rec) is True


def test_matches_recommendation_tolerates_surrounding_whitespace() -> None:
    rec = recommend_subdomain(root_domain="example.com", campaign_class="warmup")
    assert matches_recommendation("  warmup.example.com  ", rec) is True


def test_matches_recommendation_false_for_a_different_domain() -> None:
    rec = recommend_subdomain(root_domain="example.com", campaign_class="warmup")
    assert matches_recommendation("cold.example.com", rec) is False


def test_matches_recommendation_false_for_the_bare_root_domain() -> None:
    """Sending the recommended campaign class from the root domain instead
    of its subdomain is exactly the failure this check exists to catch."""
    rec = recommend_subdomain(root_domain="example.com", campaign_class="warmup")
    assert matches_recommendation("example.com", rec) is False
