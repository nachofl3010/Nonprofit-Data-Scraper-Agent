from datetime import date, datetime, timezone

from src.categorise import apply_rules
from src.schema import (
    RFP, Contacts, Evidenced, FinancialYear, Financials, GeneralContact, Identity, NewsItem,
    NonprofitProfile, OpenRole, Person, Revenue, Signals, Sourced, SourcedCauseArea, Technology, Tool,
)
from src.views import CSV_COLUMNS, funder_view, lead_score, revenue_trend, sales_view, to_csv_row, why_now

TODAY = date(2026, 9, 25)
U = "https://www.riverbendfoodbank.org/"


def person(name, title, email=None):
    return Person(name=name, title=title, email=email, linkedin=None, source_url=U, confidence="high", evidence=None)


def make_profile() -> NonprofitProfile:
    p = NonprofitProfile(
        input="riverbendfoodbank.org", resolved_url=U, status="ok",
        extracted_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
        identity=Identity(name=Sourced[str](value="Riverbend Food Bank", source_url=U, confidence="high"),
                          website=U,
                          cause_area=SourcedCauseArea(value="human_services", source_url=U, confidence="high")),
        financials=Financials(
            annual_revenue=Evidenced[Revenue](value=Revenue(amount=4_600_000, currency="USD", fiscal_year=2025),
                                              source_url=U, confidence="high", evidence="Total revenue $4.6M"),
            financial_history=[FinancialYear(fiscal_year=y, revenue=r, expenses=None, source_url=U)
                               for y, r in [(2025, 4_600_000), (2024, 4_200_000), (2022, 3_000_000)]],
        ),
        contacts=Contacts(
            leadership=[person("Ana Ruiz", "Executive Director", "ana@riverbend.org"),
                        person("Tom Lee", "Board Chair"),
                        person("Kim Park", "Director of Development")],
            general_contact=GeneralContact(email="info@riverbend.org", phone=None, contact_page_url=None, source_url=U),
        ),
        signals=Signals(
            open_rfps=[RFP(title="Donor CRM replacement", due_date="2026-10-15", url=None, source_url=U,
                           confidence="high", evidence="RFP")],
            open_roles=[OpenRole(title="Database Manager", url=None, source_url=U, confidence="high", evidence=None),
                        OpenRole(title="Warehouse Associate", url=None, source_url=U, confidence="high", evidence=None)],
            recent_news=[NewsItem(headline="New mobile market opens", date="2026-09-01", url=None, summary=None, source_url=U),
                         NewsItem(headline="Old story", date="2026-02-01", url=None, summary=None, source_url=U)],
        ),
        technology=Technology(tech_stack=[Tool(tool="Blackbaud eTapestry", category="donor_crm", source_url=U, matched="etapestry.com"),
                                          Tool(tool="Mailchimp", category="email_marketing", source_url=U, matched="list-manage.com")]),
    )
    apply_rules(p, TODAY)
    return p


def test_lead_score_arithmetic():
    score, items = lead_score(make_profile(), TODAY)
    assert {i.signal: i.points for i in items} == {
        "open_rfp": 3, "buying_signal_role": 2, "recent_news": 1,
        "competitor_tool": 1, "size_fit": 1, "reachable_decision_maker": 1,
    }
    assert score == 9


def test_small_org_without_signals_scores_low():
    p = make_profile()
    p.signals = Signals()
    p.technology = Technology()
    p.financials.annual_revenue.value.amount = 200_000
    apply_rules(p, TODAY)
    score, items = lead_score(p, TODAY)
    assert score == 0  # -1 too small, +1 reachable
    assert why_now(items).startswith("No active timing signals; nurture.")


def test_why_now_template():
    _, items = lead_score(make_profile(), TODAY)
    text = why_now(items)
    assert text.startswith("Why now: open RFP 'Donor CRM replacement' (due 2026-10-15); hiring: Database Manager; "
                           "in the news 2026-09-01: 'New mobile market opens'.")
    assert "Fit: uses Blackbaud eTapestry (migration target); revenue $4.6M in target band" in text


def test_sales_view_groups_contacts_board_last():
    v = sales_view(make_profile(), TODAY)
    assert list(v["contacts_by_role"]) == ["executive", "fundraising", "board"]
    assert v["buying_signal_roles"] == [{"title": "Database Manager", "url": None}]


def test_funder_view_trend():
    p = make_profile()
    assert revenue_trend(p) == "growing (+53% 2022->2025)"
    v = funder_view(p)
    assert v["financial_health"]["size_band"] == "$500k-$5M"


def test_csv_row_is_flat_and_clean():
    p = make_profile()
    row = to_csv_row(p, sales_view(p, TODAY), "output/riverbendfoodbank-org.json")
    assert list(row) == CSV_COLUMNS
    assert all(isinstance(v, str) and v != "None" for v in row.values())
    assert row["executive_email"] == "ana@riverbend.org"
    assert row["finance_name"] == ""
    assert row["tech_stack"] == "Blackbaud eTapestry; Mailchimp"
    assert row["donor_crm"] == "Blackbaud eTapestry"
    assert row["n_open_rfps"] == "1" and row["annual_revenue"] == "4600000"
