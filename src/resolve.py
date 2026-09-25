"""Input resolution (URL or name -> verified homepage) and the ProPublica registry lookup."""
from __future__ import annotations

import difflib
import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from src.fetch import FetchResult, fetch
from src.parse import html_to_text
from src.schema import Evidenced, FinancialYear, NonprofitProfile, Revenue, Sourced, SourcedCauseArea

TRACKING_PARAM = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_ga|_gl|igshid|ref|source)$", re.I)
DOMAIN_LIKE = re.compile(r"^[\w-]+(\.[\w-]+)+(:\d+)?([/?#].*)?$")
# Search results we never treat as an org's own website.
NOT_OFFICIAL = (
    "wikipedia.org", "facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "reddit.com", "charitynavigator.org", "guidestar.org", "candid.org",
    "propublica.org", "causeiq.com", "greatnonprofits.org", "charitywatch.org", "give.org",
    "idealist.org", "glassdoor.com", "indeed.com", "zoominfo.com", "crunchbase.com", "yelp.com",
    "bloomberg.com", "forbes.com", "nytimes.com", "cnn.com", "bbc.co.uk", "bbc.com",
    "theguardian.com", "apnews.com", "reuters.com", "charitycommission.gov.uk", "gov.uk", "irs.gov",
)
NAME_STOPWORDS = {"the", "of", "for", "and", "a", "an", "inc", "ltd", "llc", "co", "org", "nonprofit"}
PROPUBLICA = "https://projects.propublica.org/nonprofits"


class Resolution(BaseModel):
    input: str
    url: str | None = None
    method: str | None = None  # url | search
    homepage: FetchResult | None = None
    error: str | None = None
    notes: list[str] = []


# ---------------------------------------------------------------- URLs

def is_url(s: str) -> bool:
    s = s.strip()
    return "://" in s or (" " not in s and bool(DOMAIN_LIKE.match(s)))


def normalise_url(s: str) -> str:
    s = s.strip()
    if "://" not in s:
        s = "https://" + s
    p = urlparse(s)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)])
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", "", query, ""))


def fetch_homepage(url: str) -> FetchResult:
    fr = fetch(url)
    if not fr.ok and fr.status is None and url.startswith("https://"):
        fallback = fetch("http://" + url[len("https://"):])
        if fallback.ok:
            return fallback
    return fr


def resolve_input(raw: str) -> Resolution:
    raw = raw.strip()
    if not raw:
        return Resolution(input=raw, error="empty input")
    if not is_url(raw):
        return resolve_name(raw)
    url = normalise_url(raw)
    fr = fetch_homepage(url)
    if not fr.ok:
        return Resolution(input=raw, url=url, method="url", homepage=fr,
                          error=f"could not load {url}: {fr.error}")
    return Resolution(input=raw, url=normalise_url(fr.final_url or url), method="url", homepage=fr)


# ---------------------------------------------------------------- name -> URL

def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 2 and t not in NAME_STOPWORDS]


def name_matches(name: str, page_text: str) -> bool:
    """Do most of the name's words appear on the page? Fuzzy per word, so typos still match."""
    wanted = _tokens(name)
    if not wanted:
        return False
    have = set(_tokens(page_text))
    hits = sum(
        1 for w in wanted
        if w in have or any(abs(len(w) - len(h)) <= 2 and difflib.SequenceMatcher(None, w, h).ratio() >= 0.8
                            for h in have)
    )
    return hits >= max(1, round(len(wanted) * 2 / 3))


def page_identity_text(html: str) -> str:
    """Title, h1, og:site_name + the start of the visible text: where an org names itself."""
    soup = BeautifulSoup(html, "lxml")
    bits = [soup.title.get_text(" ") if soup.title else ""]
    bits += [h.get_text(" ") for h in soup.find_all("h1")[:3]]
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content"):
        bits.append(og["content"])
    return " ".join(bits) + " " + html_to_text(html, "")[:3000]


def _is_official_candidate(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return not any(host == d or host.endswith("." + d) for d in NOT_OFFICIAL)


def resolve_name(name: str) -> Resolution:
    key = os.getenv("SEARCH_API_KEY")
    res = Resolution(input=name, method="search")
    if not key:
        res.error = "input looks like a name, and no SEARCH_API_KEY is set; please provide the website URL"
        return res
    try:
        r = httpx.post("https://google.serper.dev/search", timeout=15.0,
                       headers={"X-API-KEY": key, "Content-Type": "application/json"},
                       json={"q": f"{name} nonprofit official website", "num": 5})
        r.raise_for_status()
        results = [o["link"] for o in r.json().get("organic", [])[:5]]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        res.error = f"web search failed ({type(e).__name__}); please provide the website URL"
        return res
    candidates = [u for u in results if _is_official_candidate(u)]
    res.notes.append(f"search results: {len(results)}, after dropping directories/social/news: {len(candidates)}")
    for link in candidates[:2]:
        p = urlparse(link)
        home = f"{p.scheme}://{p.netloc}/"
        fr = fetch_homepage(home)
        if fr.ok and name_matches(name, page_identity_text(fr.text)):
            res.url, res.homepage = normalise_url(fr.final_url or home), fr
            res.notes.append(f"verified {home}: org name found on page")
            return res
        res.notes.append(f"rejected {home}: {'unreachable' if not fr.ok else 'name not found on page'}")
    res.error = f"could not find a verified official website for {name!r}; please provide the URL"
    return res


# ---------------------------------------------------------------- ProPublica (IRS 990 data, US only)

class Registry(BaseModel):
    ein: str
    name: str
    city: str | None
    state: str | None
    ntee_code: str | None
    subsection_code: int | None
    history: list[FinancialYear]
    source_url: str
    matched_by: str  # ein | name


def _pp_json(url: str) -> dict | None:
    fr = fetch(url, check_robots=False)  # public JSON API, not crawling
    if not fr.ok:
        return None
    try:
        return json.loads(fr.content)
    except ValueError:
        return None


def _norm_org_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    s = re.sub(r"\b(inc|incorporated|corp|corporation|co|ltd|llc|the|of|usa|us)\b", " ", s)
    return " ".join(s.split())


def us_state(hq_location: str | None) -> str | None:
    m = re.search(r",\s*([A-Z]{2})\b", hq_location or "")
    return m.group(1) if m else None


def us_city(hq_location: str | None) -> str | None:
    return hq_location.split(",")[0].strip() if hq_location and us_state(hq_location) else None


def propublica_lookup(name: str | None, ein: str | None, state: str | None,
                      city: str | None = None) -> tuple[Registry | None, str]:
    """Only returns a registry record when the match is confident; otherwise (None, reason).
    Name matches must be unique: many chapters and affiliates share a parent's name."""
    digits = re.sub(r"\D", "", ein or "")
    matched_by = "ein"
    if len(digits) != 9:
        if not name:
            return None, "no EIN or name to look up"
        q = urlencode({"q": name, **({"state[id]": state} if state else {})})
        data = _pp_json(f"{PROPUBLICA}/api/v2/search.json?{q}")
        if not data:
            return None, "no ProPublica search results"
        target = _norm_org_name(name)
        matches = [
            org for org in data.get("organizations", [])[:10]
            if max(difflib.SequenceMatcher(None, target, _norm_org_name(n or "")).ratio()
                   for n in (org.get("name"), org.get("sub_name"))) >= 0.9
            and (not state or org.get("state") == state)
        ]
        if not state:  # nothing to narrow by: only an exact (normalised) name will do
            matches = [o for o in matches if target in (_norm_org_name(o.get("name") or ""),
                                                       _norm_org_name(o.get("sub_name") or ""))]
        if len(matches) > 1 and city:
            matches = [o for o in matches if (o.get("city") or "").lower() == city.lower()] or matches
        if len(matches) != 1:
            why = "no name match" if not matches else f"{len(matches)} registry entities share this name"
            return None, f"no confident registry match for {name!r}: {why}"
        digits, matched_by = str(matches[0]["ein"]).zfill(9), "name"

    data = _pp_json(f"{PROPUBLICA}/api/v2/organizations/{digits}.json")
    if not data or "organization" not in data:
        return None, f"EIN {digits} not found in ProPublica"
    org = data["organization"]
    src = f"{PROPUBLICA}/organizations/{digits}"
    history = [
        FinancialYear(fiscal_year=f["tax_prd_yr"], revenue=f.get("totrevenue"),
                      expenses=f.get("totfuncexpns"), source_url=src)
        for f in data.get("filings_with_data", []) if f.get("tax_prd_yr")
    ]
    # The org record carries the latest return's revenue, often newer than the parsed filings.
    tp, rev = org.get("tax_period"), org.get("revenue_amount")
    if tp and rev is not None and all(h.fiscal_year < int(tp[:4]) for h in history):
        history.append(FinancialYear(fiscal_year=int(tp[:4]), revenue=rev, expenses=None, source_url=src))
    history.sort(key=lambda h: -h.fiscal_year)
    reg = Registry(ein=f"{digits[:2]}-{digits[2:]}", name=org.get("name") or "", city=org.get("city"),
                   state=org.get("state"), ntee_code=org.get("ntee_code"),
                   subsection_code=org.get("subsection_code"), history=history[:8],
                   source_url=src, matched_by=matched_by)
    return reg, f"matched {reg.name} (EIN {reg.ein}) by {matched_by}"


def merge_registry(p: NonprofitProfile, reg: Registry, ntee_to_cause) -> None:
    """Registry data is authoritative for EIN, tax status and filed financials."""
    ident, fin = p.identity, p.financials
    if ident.registration_id is None or re.sub(r"\D", "", ident.registration_id.value) != reg.ein.replace("-", ""):
        ident.registration_id = Evidenced[str](value=reg.ein, source_url=reg.source_url, confidence="high",
                                               evidence=f"IRS record: {reg.name}, EIN {reg.ein}")
    if ident.legal_name is None and reg.name:
        ident.legal_name = Sourced[str](value=reg.name, source_url=reg.source_url, confidence="high")
    if reg.subsection_code:
        fin.tax_status = Sourced[str](value=f"501(c)({reg.subsection_code})", source_url=reg.source_url,
                                      confidence="high")
    fin.financial_history = reg.history or None
    latest = next((h for h in reg.history if h.revenue is not None), None)
    site_rev = fin.annual_revenue
    site_year = site_rev.value.fiscal_year if site_rev else None
    if latest and (site_rev is None or site_year is None or latest.fiscal_year >= site_year):
        fin.annual_revenue = Evidenced[Revenue](
            value=Revenue(amount=latest.revenue, currency="USD", fiscal_year=latest.fiscal_year),
            source_url=reg.source_url, confidence="high",
            evidence=f"IRS Form 990 total revenue, fiscal year {latest.fiscal_year}")
    cause = ntee_to_cause(reg.ntee_code)
    if cause:
        # Registry code wins for consistency; a disagreement with the site is worth a human look.
        if ident.cause_area and ident.cause_area.value != cause:
            p.flags.append(f"cause_area_disagreement:site={ident.cause_area.value},registry={cause}")
        ident.cause_area = SourcedCauseArea(value=cause, source_url=reg.source_url, confidence="high")
    p.flags.append(f"registry_match:{reg.matched_by}")
