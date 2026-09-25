from datetime import datetime, timezone

from src.categorise import ntee_to_cause_area
from src.resolve import (
    Registry, _is_official_candidate, is_url, merge_registry, name_matches, normalise_url, us_state,
)
from src.schema import Evidenced, FinancialYear, Financials, NonprofitProfile, Revenue


def test_is_url():
    assert is_url("https://www.habitat.org")
    assert is_url("habitat.org/?utm_source=x")
    assert is_url("www.charitywater.org")
    assert not is_url("charity: water")
    assert not is_url("Habitat for Humanity")


def test_normalise_url_strips_tracking_and_adds_scheme():
    assert normalise_url("habitat.org/?utm_source=newsletter&utm_medium=email&id=5#top") == "https://habitat.org/?id=5"
    assert normalise_url("HTTPS://WWW.Example.ORG") == "https://www.example.org/"
    assert normalise_url(" https://x.org/about?fbclid=abc ") == "https://x.org/about"


def test_name_matches_tolerates_typos_but_not_other_orgs():
    assert name_matches("Habitat for Humantiy", "Habitat for Humanity International | Home")
    assert name_matches("charity: water", "charity: water | Clean water for everyone")
    assert not name_matches("Red Cross", "Blue Shield of California health plans")
    assert not name_matches("the", "anything")


def test_directory_sites_rejected():
    assert not _is_official_candidate("https://en.wikipedia.org/wiki/Habitat_for_Humanity")
    assert not _is_official_candidate("https://www.charitynavigator.org/ein/123")
    assert _is_official_candidate("https://www.habitat.org/")


def test_us_state():
    assert us_state("Nashville, TN") == "TN"
    assert us_state("London, England") is None
    assert us_state(None) is None


def _registry(years):
    src = "https://projects.propublica.org/nonprofits/organizations/123456789"
    return Registry(ein="12-3456789", name="RIVERBEND FOOD BANK INC", city="Riverbend", state="TN",
                    ntee_code="K31", subsection_code=3, source_url=src, matched_by="ein",
                    history=[FinancialYear(fiscal_year=y, revenue=r, expenses=None, source_url=src) for y, r in years])


def _profile(site_year):
    p = NonprofitProfile(input="x", extracted_at=datetime.now(timezone.utc), status="ok")
    p.financials = Financials(annual_revenue=Evidenced[Revenue](
        value=Revenue(amount=5_000_000, currency="USD", fiscal_year=site_year),
        source_url="https://x.org/ar.pdf", confidence="high", evidence="Total revenue $5M"))
    return p


def test_registry_wins_ties_and_newer_years():
    p = _profile(2024)
    merge_registry(p, _registry([(2024, 4_200_000), (2023, 3_900_000)]), ntee_to_cause_area)
    assert p.financials.annual_revenue.value.amount == 4_200_000
    assert p.identity.registration_id.value == "12-3456789"
    assert p.financials.tax_status.value == "501(c)(3)"
    assert p.identity.cause_area.value == "human_services"
    assert "registry_match:ein" in p.flags


def test_newer_site_figure_beats_older_filing():
    p = _profile(2025)
    merge_registry(p, _registry([(2023, 3_900_000)]), ntee_to_cause_area)
    assert p.financials.annual_revenue.value.amount == 5_000_000
    assert len(p.financials.financial_history) == 1


def test_registry_disagreement_is_flagged():
    from src.schema import SourcedCauseArea
    p = _profile(2024)
    p.identity.cause_area = SourcedCauseArea(value="international", source_url="https://x.org/", confidence="high")
    merge_registry(p, _registry([(2024, 4_200_000)]), ntee_to_cause_area)
    assert p.identity.cause_area.value == "human_services"
    assert "cause_area_disagreement:site=international,registry=human_services" in p.flags
