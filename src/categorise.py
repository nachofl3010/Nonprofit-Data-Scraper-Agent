"""Deterministic rules applied after extraction: size band, buyer role, buying-signal
roles, NTEE -> cause area, and date filters. Same input -> same output across 500k orgs.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from src.schema import BuyerRole, CauseArea, NewsItem, NonprofitProfile, RFP, SizeBand

NEWS_MAX_AGE_DAYS = 365

# Approximate FX to USD, only used to pick a size band. Refresh yearly.
FX_TO_USD = {"USD": 1.0, "GBP": 1.27, "EUR": 1.08, "CAD": 0.73, "AUD": 0.66, "NZD": 0.60,
             "CHF": 1.12, "SEK": 0.095, "NOK": 0.093, "DKK": 0.145, "INR": 0.012, "ZAR": 0.055}

# NTEE major group letter -> our cause area (taxonomy "ntee-major-v1").
NTEE_TO_CAUSE: dict[str, CauseArea] = {
    "A": "arts_culture", "B": "education", "C": "environment", "D": "animals",
    "E": "health", "G": "health", "H": "health", "F": "mental_health",
    **{k: "human_services" for k in "IJKLMNOP"},
    "Q": "international", "R": "civil_rights", "S": "community_development", "W": "community_development",
    "T": "philanthropy", "U": "science_research", "V": "science_research", "X": "religion",
    "Y": "other", "Z": "other",
}

# Checked in order; first match wins. Board first so "Board Chair" never reads as executive;
# functional roles before executive so "Chief Development Officer" is fundraising.
ROLE_RULES: list[tuple[BuyerRole, re.Pattern]] = [
    ("board", re.compile(r"\bboard\b|\btrustee|\bchair(man|woman|person)?\b|\bvice[- ]chair|\btreasurer\b|"
                         r"\bdirector emerit", re.I)),
    # "development" means fundraising in nonprofit titles, except these program-side uses
    ("fundraising", re.compile(r"(?<!youth )(?<!community )(?<!program )(?<!programme )(?<!professional )"
                               r"(?<!software )develop|fundrais|advancement|philanthrop|\bdonor|major gift|chief revenue|revenue officer|"
                               r"\bgrants?\b|partnership|stewardship|\bgiving\b|income generation", re.I)),
    ("finance", re.compile(r"\bcfo\b|financ|controller|comptroller|accounting|accountant|\bbudget", re.I)),
    ("operations_it", re.compile(r"\bcoo\b|operat|\bcto\b|\bcio\b|technolog|information|\bdata\b|database|"
                                 r"\bcrm\b|\bsystems?\b|digital|salesforce|(?-i:\bIT\b)", re.I)),
    ("executive", re.compile(r"\bceo\b|chief executive|executive director|(?<!vice )(?<!vice-)\bpresident\b|"
                             r"founder|managing director|general manager|director general|secretary general", re.I)),
]

# Job titles that suggest a tooling or vendor decision is coming.
BUYING_SIGNAL = re.compile(
    r"(?<!youth )(?<!community )(?<!program )(?<!programme )(?<!professional )"
    r"development (director|manager|officer)|director of development|chief development|"
    r"fundrais\w* (director|manager|operations)|director of fundrais|head of fundrais|"
    r"\bcrm\b|database|data (manager|analyst|administrator)|salesforce|raiser'?s edge|gift processing|"
    r"donor (data|systems|operations|database)|advancement services|"
    r"grants? (manager|director)|"
    r"(director|head|vp|vice president) of operations|operations director|\bcoo\b|chief operating|"
    r"information technology|technology (director|manager)|\bcto\b|\bcio\b|systems (manager|administrator)|"
    r"financ\w* (director|manager)|director of financ|\bcfo\b|chief financial|controller|"
    r"(?-i:\bIT\b)",  # "IT" only in capitals, so the word "it" never matches
    re.I,
)


def size_band(amount: float | None, currency: str | None = "USD") -> SizeBand:
    if amount is None:
        return "unknown"
    rate = FX_TO_USD.get((currency or "USD").upper())
    if rate is None:
        return "unknown"
    usd = amount * rate
    if usd < 500_000:
        return "<$500k"
    if usd < 5_000_000:
        return "$500k-$5M"
    if usd < 50_000_000:
        return "$5M-$50M"
    return ">$50M"


def buyer_role(title: str | None) -> BuyerRole:
    if not title:
        return "other"
    for role, pattern in ROLE_RULES:
        if pattern.search(title):
            return role
    return "other"


def is_buying_signal(title: str | None) -> bool:
    return bool(title) and bool(BUYING_SIGNAL.search(title))


def ntee_to_cause_area(code: str | None) -> CauseArea | None:
    return NTEE_TO_CAUSE.get(code[0].upper()) if code else None


def parse_date(s: str | None) -> date | None:
    """ISO date, or YYYY-MM / YYYY (-> first day). None if unparseable."""
    if not s:
        return None
    s = s.strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def filter_recent_news(items: list[NewsItem], today: date) -> tuple[list[NewsItem], int]:
    """Keep dated items from the last ~12 months. Undated items are dropped: 'recent'
    has to be verifiable. Returns (kept, dropped_count)."""
    kept = [n for n in items
            if (d := parse_date(n.date)) and 0 <= (today - d).days <= NEWS_MAX_AGE_DAYS]
    kept.sort(key=lambda n: parse_date(n.date), reverse=True)
    return kept, len(items) - len(kept)


def filter_open_rfps(items: list[RFP], today: date) -> tuple[list[RFP], int]:
    """Drop RFPs whose due date has passed; keep undated ones (often 'rolling')."""
    kept = [r for r in items if (d := parse_date(r.due_date)) is None or d >= today]
    return kept, len(items) - len(kept)


def apply_rules(p: NonprofitProfile, today: date) -> None:
    rev = p.financials.annual_revenue
    p.financials.size_band = size_band(rev.value.amount, rev.value.currency) if rev else "unknown"
    for person in p.contacts.leadership:
        person.buyer_role = buyer_role(person.title)
    for role in p.signals.open_roles:
        role.is_buying_signal = is_buying_signal(role.title)
    p.signals.recent_news, dropped_news = filter_recent_news(p.signals.recent_news, today)
    if dropped_news:
        p.errors.append(f"news filter: dropped {dropped_news} item(s) older than 12 months or undated")
    p.signals.open_rfps, dropped_rfps = filter_open_rfps(p.signals.open_rfps, today)
    if dropped_rfps:
        p.errors.append(f"rfp filter: dropped {dropped_rfps} RFP(s) past their due date")
