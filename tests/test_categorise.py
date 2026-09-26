from datetime import date

import pytest

from src.categorise import (
    buyer_role, filter_open_rfps, filter_recent_news, is_buying_signal, ntee_to_cause_area,
    parse_date, size_band,
)
from src.schema import RFP, NewsItem

TODAY = date(2026, 9, 25)


@pytest.mark.parametrize("title,role", [
    ("Executive Director", "executive"),
    ("President & CEO", "executive"),
    ("Founder and Chief Executive Officer", "executive"),
    ("Secretary General", "executive"),
    ("Chief Development Officer", "fundraising"),
    ("Director of Development & Communications", "fundraising"),
    ("Grants Manager", "fundraising"),
    ("Youth Development Coordinator", "other"),
    ("Chief Financial Officer", "finance"),
    ("Controller", "finance"),
    ("Chief Operating Officer", "operations_it"),
    ("IT Manager", "operations_it"),
    ("Database Administrator", "operations_it"),
    ("Board Chair", "board"),
    ("Trustee", "board"),
    ("Treasurer", "board"),
    ("Vice President of Programs", "other"),
    ("Program Coordinator", "other"),
    (None, "other"),
])
def test_buyer_role(title, role):
    assert buyer_role(title) == role


@pytest.mark.parametrize("title,signal", [
    ("Development Director", True),
    ("CRM Manager", True),
    ("Salesforce Administrator", True),
    ("Grants Manager", True),
    ("Director of Operations", True),
    ("IT Support Specialist", True),
    ("Finance Director", True),
    ("Youth Development Manager", False),
    ("Case Manager", False),
    ("Make it happen coordinator", False),
    ("Volunteer Coordinator", False),
])
def test_buying_signal(title, signal):
    assert is_buying_signal(title) is signal


def test_size_band_thresholds():
    assert size_band(None) == "unknown"
    assert size_band(499_999) == "<$500k"
    assert size_band(500_000) == "$500k-$5M"
    assert size_band(5_000_000) == "$5M-$50M"
    assert size_band(50_000_000) == ">$50M"
    assert size_band(3_000_000, "GBP") == "$500k-$5M"
    assert size_band(4_500_000, "GBP") == "$5M-$50M"  # converted, not raw
    assert size_band(1_000_000, "XYZ") == "unknown"


def test_ntee_mapping():
    assert ntee_to_cause_area("K31") == "human_services"
    assert ntee_to_cause_area("Q33") == "international"
    assert ntee_to_cause_area("F30") == "mental_health"
    assert ntee_to_cause_area(None) is None


def test_parse_date_formats():
    assert parse_date("2026-03-14") == date(2026, 3, 14)
    assert parse_date("2026-03") == date(2026, 3, 1)
    assert parse_date("2025") == date(2025, 1, 1)
    assert parse_date("March 2025") is None


def test_news_filter_drops_old_and_undated_and_sorts():
    items = [
        NewsItem(headline="old", date="2024-06-01", url=None, summary=None, source_url="u"),
        NewsItem(headline="undated", date=None, url=None, summary=None, source_url="u"),
        NewsItem(headline="recent", date="2026-01-10", url=None, summary=None, source_url="u"),
        NewsItem(headline="newest", date="2026-09-01", url=None, summary=None, source_url="u"),
        NewsItem(headline="future typo", date="2027-01-01", url=None, summary=None, source_url="u"),
    ]
    kept, dropped = filter_recent_news(items, TODAY)
    assert [n.headline for n in kept] == ["newest", "recent"]
    assert dropped == 3


def test_rfp_filter_keeps_open_and_undated():
    rfps = [RFP(title=t, due_date=d, url=None, source_url="u", confidence="high", evidence=None)
            for t, d in [("past", "2026-01-01"), ("open", "2026-10-15"), ("rolling", None)]]
    kept, dropped = filter_open_rfps(rfps, TODAY)
    assert [r.title for r in kept] == ["open", "rolling"] and dropped == 1


def test_chief_revenue_officer_is_fundraising():
    assert buyer_role("Chief Revenue Officer") == "fundraising"


def test_board_appointments_are_not_leadership_changes():
    from src.categorise import drop_board_appointments
    from src.schema import BuyerSignal

    def sig(t, desc):
        return BuyerSignal(type=t, description=desc, date="2026-09", source_url="u", confidence="high", evidence=desc)

    kept, dropped = drop_board_appointments([
        sig("leadership_change", "New members welcomed to the Board of Governors"),
        sig("leadership_change", "Appointment of a new Chief Financial Officer"),
        sig("strategic_plan", "Board approves the 2026-2030 strategic plan"),
    ])
    assert dropped == 1
    assert [s.description for s in kept] == ["Appointment of a new Chief Financial Officer",
                                             "Board approves the 2026-2030 strategic plan"]
