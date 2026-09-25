"""Views: selections and rankings computed from the core profile. No crawling, no LLM.

sales_view  - primary user: vendors selling to nonprofits (donor CRM, fundraising software).
funder_view - secondary user: foundations, grantmakers, CSR teams.
Adding a customer type = adding a function here; the core profile doesn't change.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from src.categorise import parse_date
from src.schema import NonprofitProfile, Person

# Who the sales view scores for: a donor-CRM / fundraising-software vendor.
# Retarget by editing these; nothing upstream changes.
TARGET = {
    "competitor_categories": {"donor_crm"},
    "ideal_size_bands": {"$500k-$5M", "$5M-$50M"},
    "too_small_bands": {"<$500k"},
}
RECENT_NEWS_DAYS = 90
ROLE_ORDER = ["executive", "fundraising", "finance", "operations_it", "other", "board"]
DECISION_ROLES = {"executive", "fundraising", "finance", "operations_it"}


class ScoreItem(BaseModel):
    signal: str
    points: int
    detail: str
    timing: bool  # timing signals feed "why now"; the rest are fit signals


def _money(amount: float, currency: str) -> str:
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency, currency + " ")
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if amount >= div:
            return f"{sym}{amount / div:.1f}{suffix}"
    return f"{sym}{amount:.0f}"


def lead_score(p: NonprofitProfile, today: date) -> tuple[int, list[ScoreItem]]:
    items: list[ScoreItem] = []
    s, fin = p.signals, p.financials

    if s.open_rfps:
        r = s.open_rfps[0]
        due = f" (due {r.due_date})" if r.due_date else ""
        items.append(ScoreItem(signal="open_rfp", points=3, timing=True, detail=f"open RFP '{r.title}'{due}"))
    signal_roles = [r for r in s.open_roles if r.is_buying_signal]
    if signal_roles:
        titles = ", ".join(r.title for r in signal_roles[:2])
        items.append(ScoreItem(signal="buying_signal_role", points=2, timing=True, detail=f"hiring: {titles}"))
    fresh = [n for n in s.recent_news if (d := parse_date(n.date)) and (today - d).days <= RECENT_NEWS_DAYS]
    if fresh:
        n = fresh[0]
        items.append(ScoreItem(signal="recent_news", points=1, timing=True,
                               detail=f"in the news {n.date}: '{n.headline}'"))

    crm = [t.tool for t in p.technology.tech_stack if t.category in TARGET["competitor_categories"]]
    if crm:
        items.append(ScoreItem(signal="competitor_tool", points=1, timing=False,
                               detail=f"uses {', '.join(crm)} (migration target)"))
    rev = fin.annual_revenue
    if fin.size_band in TARGET["ideal_size_bands"]:
        items.append(ScoreItem(signal="size_fit", points=1, timing=False,
                               detail=f"revenue {_money(rev.value.amount, rev.value.currency)} in target band"))
    elif fin.size_band in TARGET["too_small_bands"]:
        items.append(ScoreItem(signal="size_too_small", points=-1, timing=False,
                               detail="revenue under $500k (limited budget)"))
    reachable = next((x for x in p.contacts.leadership if x.buyer_role in DECISION_ROLES and x.email), None)
    if reachable:
        items.append(ScoreItem(signal="reachable_decision_maker", points=1, timing=False,
                               detail=f"direct email for {reachable.name} ({reachable.title})"))
    return sum(i.points for i in items), items


def why_now(items: list[ScoreItem]) -> str:
    """Templated, so it reads the same way across 500k orgs."""
    timing = [i.detail for i in items if i.timing]
    fit = [i.detail for i in items if not i.timing]
    out = ("Why now: " + "; ".join(timing) + ".") if timing else "No active timing signals; nurture."
    if fit:
        out += " Fit: " + "; ".join(fit) + "."
    return out


def contacts_by_role(p: NonprofitProfile) -> dict[str, list[Person]]:
    groups: dict[str, list[Person]] = {r: [] for r in ROLE_ORDER}
    for person in p.contacts.leadership:
        groups[person.buyer_role].append(person)
    return {r: people for r, people in groups.items() if people}


def _val(sourced):
    return sourced.value if sourced is not None else None


def sales_view(p: NonprofitProfile, today: date) -> dict:
    score, items = lead_score(p, today)
    rev = p.financials.annual_revenue
    return {
        "name": _val(p.identity.name),
        "website": p.identity.website,
        "status": p.status,
        "flags": p.flags,
        "cause_area": _val(p.identity.cause_area),
        "size_band": p.financials.size_band,
        "annual_revenue": rev.value.model_dump() if rev else None,
        "hq_location": _val(p.identity.hq_location),
        "lead_score": score,
        "score_breakdown": [i.model_dump() for i in items],
        "why_now": why_now(items),
        "tech_stack": [f"{t.tool} ({t.category})" for t in p.technology.tech_stack],
        "contacts_by_role": {
            role: [{"name": x.name, "title": x.title, "email": x.email, "linkedin": x.linkedin} for x in people]
            for role, people in contacts_by_role(p).items()
        },
        "general_contact": p.contacts.general_contact.model_dump() if p.contacts.general_contact else None,
        "open_rfps": [r.model_dump(exclude={"evidence"}) for r in p.signals.open_rfps],
        "buying_signal_roles": [{"title": r.title, "url": r.url} for r in p.signals.open_roles if r.is_buying_signal],
        "recent_news": [{"date": n.date, "headline": n.headline, "url": n.url} for n in p.signals.recent_news],
    }


def revenue_trend(p: NonprofitProfile) -> str:
    """Latest filing vs ~3 years earlier: growing / stable / declining / unknown."""
    hist = [h for h in (p.financials.financial_history or []) if h.revenue]
    if len(hist) < 2:
        return "unknown"
    latest = hist[0]
    base = next((h for h in hist if latest.fiscal_year - h.fiscal_year >= 3), hist[-1])
    change = (latest.revenue - base.revenue) / base.revenue
    label = "growing" if change > 0.1 else "declining" if change < -0.1 else "stable"
    return f"{label} ({change:+.0%} {base.fiscal_year}->{latest.fiscal_year})"


def funder_view(p: NonprofitProfile) -> dict:
    ident, fin = p.identity, p.financials
    rev = fin.annual_revenue
    return {
        "name": _val(ident.name),
        "website": ident.website,
        "mission": _val(ident.mission),
        "cause_area": _val(ident.cause_area),
        "geography_served": ident.geography_served.value.model_dump() if ident.geography_served else None,
        "programs": [pr.model_dump() for pr in ident.programs],
        "registration_id": _val(ident.registration_id),
        "tax_status": _val(fin.tax_status),
        "financial_health": {
            "annual_revenue": rev.value.model_dump() if rev else None,
            "size_band": fin.size_band,
            "trend": revenue_trend(p),
            "history": [h.model_dump(exclude={"source_url"}) for h in (fin.financial_history or [])],
        },
        "impact_metrics": [m.value for m in ident.impact_metrics],
        "existing_funders": [f"{f.name} ({f.type})" for f in p.signals.funders_and_partners],
    }


CSV_COLUMNS = [
    "name", "website", "status", "lead_score", "why_now", "cause_area", "size_band", "annual_revenue",
    "currency", "fiscal_year", "hq_location", "country", "registration_id",
    "executive_name", "executive_title", "executive_email",
    "fundraising_name", "fundraising_title", "fundraising_email",
    "finance_name", "finance_title", "finance_email",
    "operations_it_name", "operations_it_title", "operations_it_email",
    "general_email", "general_phone", "tech_stack", "donor_crm",
    "n_open_rfps", "n_buying_signal_roles", "n_recent_news", "latest_news_date",
    "flags", "extracted_at", "profile_json",
]


def to_csv_row(p: NonprofitProfile, sales: dict, profile_path: str = "") -> dict[str, str]:
    """One flat row for CRM import: top contact per buyer role, lists joined with '; ',
    empty cells as '' (never 'None')."""
    rev = p.financials.annual_revenue
    gc = p.contacts.general_contact
    row: dict[str, object] = {
        "name": sales["name"], "website": sales["website"], "status": p.status,
        "lead_score": sales["lead_score"], "why_now": sales["why_now"],
        "cause_area": sales["cause_area"], "size_band": p.financials.size_band,
        "annual_revenue": int(rev.value.amount) if rev else None,
        "currency": rev.value.currency if rev else None,
        "fiscal_year": rev.value.fiscal_year if rev else None,
        "hq_location": _val(p.identity.hq_location), "country": _val(p.identity.country),
        "registration_id": _val(p.identity.registration_id),
        "general_email": gc.email if gc else None, "general_phone": gc.phone if gc else None,
        "tech_stack": "; ".join(t.tool for t in p.technology.tech_stack),
        "donor_crm": "; ".join(t.tool for t in p.technology.tech_stack if t.category == "donor_crm"),
        "n_open_rfps": len(p.signals.open_rfps),
        "n_buying_signal_roles": len(sales["buying_signal_roles"]),
        "n_recent_news": len(p.signals.recent_news),
        "latest_news_date": p.signals.recent_news[0].date if p.signals.recent_news else None,
        "flags": "; ".join(p.flags),
        "extracted_at": p.extracted_at.isoformat(timespec="seconds"),
        "profile_json": profile_path,
    }
    groups = contacts_by_role(p)
    for role in ("executive", "fundraising", "finance", "operations_it"):
        people = groups.get(role, [])
        top = next((x for x in people if x.email), people[0] if people else None)
        row[f"{role}_name"] = top.name if top else None
        row[f"{role}_title"] = top.title if top else None
        row[f"{role}_email"] = top.email if top else None
    return {col: "" if row.get(col) is None else str(row[col]) for col in CSV_COLUMNS}
