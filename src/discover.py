"""Find the pages worth fetching: homepage links + sitemap URLs, scored by keyword rules.

No LLM here. Keyword scoring picks the obvious pages (about, team, careers, ...) well, and
the bounded gap-fill step in extract.py gets the unfetched remainder if key fields are missing.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urldefrag, urljoin, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from pydantic import BaseModel

from src.fetch import fetch, origin, robots_sitemaps

# category -> (weight, keywords). Keywords match at a word boundary in the URL path
# or anchor text. Order = selection priority when filling one page per category.
CATEGORIES: dict[str, tuple[int, list[str]]] = {
    "about": (10, ["about", "mission", "who-we-are", "our-story", "history", "vision", "overview"]),
    "team": (10, ["team", "staff", "leadership", "board", "our-people", "people", "directors",
                  "trustees", "governance"]),
    "financials": (9, ["annual-report", "annual_report", "annualreport", "financials", "financial",
                       "990", "accountability", "transparency", "audit", "impact-report"]),
    "careers": (8, ["careers", "jobs", "employment", "join-us", "work-with-us", "vacancies",
                    "join-our-team", "opportunities", "work-for-us"]),
    "contact": (7, ["contact", "get-in-touch", "reach-us", "locations"]),
    "programs": (7, ["programs", "programmes", "what-we-do", "our-work", "services", "impact",
                     "projects", "initiatives"]),
    "rfp": (8, ["rfp", "rfq", "procurement", "request-for-proposal", "requests-for-proposals",
                "tender", "tenders", "bids", "vendor"]),
    "news": (6, ["news", "press", "media", "blog", "stories", "updates", "announcements"]),
    "partners": (6, ["partners", "funders", "sponsors", "supporters", "our-donors"]),
    "donate": (5, ["donate", "give", "giving", "support-us", "donation"]),
}
# Word boundaries matter: /research is not /search, /accountability is not /account.
NEGATIVE_PATH = re.compile(
    r"(?:^|[/\-_])(privacy|cookies?|terms|login|log-in|signin|sign-in|register|cart|checkout|"
    r"my-account|account|wp-admin|wp-json|tag|category|author|search|newsletter|unsubscribe|feed)"
    r"(?:$|[/\-_.])"
    r"|\.(jpe?g|png|gif|svg|webp|mp4|mp3|zip|docx?|xlsx?|pptx?|css|js|ics)$",
    re.I,
)
NEGATIVE_ANCHOR = re.compile(
    r"\b(privacy|cookies?|terms|log ?in|sign ?in|cart|checkout|my account|newsletter|unsubscribe)\b", re.I)
# Careers and donate pages often live on these third-party hosts.
ALLOWED_EXTERNAL = (
    "greenhouse.io", "lever.co", "workable.com", "bamboohr.com", "idealist.org", "applytojob.com",
    "classy.org", "givebutter.com", "donorbox.org", "blackbaud.com", "networkforgood.com",
    "qgiv.com", "givelively.org", "fundraiseup.com",
)
MIN_SCORE = 3.0
MAX_SITEMAP_URLS = 500

_patterns = {
    cat: [re.compile(r"(?:^|[/\-_.\s])" + re.escape(kw).replace(r"\-", r"[-_ ]?"), re.I) for kw in kws]
    for cat, (_, kws) in CATEGORIES.items()
}


class Candidate(BaseModel):
    url: str
    anchor: str = ""
    score: float
    category: str
    source: str = "homepage"  # homepage | sitemap | feed | hop


def base_domain(host: str) -> str:
    labels = host.lower().removeprefix("www.").split(".")
    # example.org.uk / example.com.au style second-level registries
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in {"org", "co", "com", "ac", "gov", "net", "or"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_allowed(url: str, site_base: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    if base_domain(host) == site_base:
        return True
    return any(host == h or host.endswith("." + h) for h in ALLOWED_EXTERNAL)


def score_link(url: str, anchor: str = "", this_year: int | None = None) -> tuple[float, str]:
    """(score, category). Negative score = skip."""
    path = urlparse(url).path.lower()
    if NEGATIVE_PATH.search(path) or NEGATIVE_ANCHOR.search(anchor):
        return -10.0, "skip"
    # The last path segment says what the page is (/about/financials is a financials page);
    # parent segments only hint at it.
    segs = [seg for seg in path.split("/") if seg]
    last, parent = "/" + (segs[-1] if segs else ""), "/" + "/".join(segs[:-1])
    best, best_cat = 0.0, "other"
    for cat, (weight, _) in CATEGORIES.items():
        pats = _patterns[cat]
        if any(p.search(last) for p in pats):
            s = weight
        elif anchor and any(p.search(" " + anchor.lower()) for p in pats):
            s = weight * 0.8
        elif any(p.search(parent) for p in pats):
            s = weight * 0.5
        else:
            s = 0.0
        if s > best:
            best, best_cat = s, cat
    if path.endswith(".pdf"):
        best += 2
        if best_cat in ("other", "news"):
            best_cat = "financials" if re.search(r"report|financ|990|audit", path + anchor, re.I) else best_cat
    elif path.endswith(".csv"):
        best += 1
    # Recent reports beat old ones. Years from the file name / anchor only: upload folders
    # (/uploads/2024/04/) say when a file was posted, not what year it covers.
    if best_cat == "financials":
        years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", last + " " + anchor)]
        if years:
            best += max(0, 2 - ((this_year or date.today().year) - max(years)))
    # Deep URLs are usually individual posts, not index pages.
    best -= max(0, len(segs) - 2)
    # Long slugs are articles ("inspiring-young-people-to-take-action"), not index pages.
    slug_words = len(re.split(r"[-_]", segs[-1].rsplit(".", 1)[0])) if segs else 0
    if slug_words > 4:
        best -= 3
    return best, best_cat


def homepage_links(html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if url.startswith("http"):
            anchor = a.get_text(" ", strip=True) or a.get("aria-label", "") or a.get("title", "")
            links.append((url, anchor[:80]))
    return links


MAX_REPORT_AGE_YEARS = 3


def pdf_links(html: str, page_url: str, this_year: int | None = None) -> list[Candidate]:
    """Report PDFs linked from a page (used one hop from the financials page). Reports whose
    newest year is older than MAX_REPORT_AGE_YEARS are skipped: stale data costs tokens and misleads."""
    site = base_domain(urlparse(page_url).netloc)
    oldest_ok = (this_year or date.today().year) - MAX_REPORT_AGE_YEARS
    out = []
    for url, anchor in homepage_links(html, page_url):
        years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", url.rsplit("/", 1)[-1] + " " + anchor)]
        if years and max(years) < oldest_ok:
            continue
        if urlparse(url).path.lower().endswith(".pdf") and host_allowed(url, site):
            score, cat = score_link(url, anchor)
            if cat == "financials" and score > 0:
                out.append(Candidate(url=url, anchor=anchor, score=score, category="financials_pdf", source="hop"))
    return sorted(out, key=lambda c: -c.score)


def feed_links(html: str, base_url: str) -> list[str]:
    """RSS/Atom feeds advertised in <head>: a cheap, dated news source (XML)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for link in soup.find_all("link", attrs={"rel": "alternate"}):
        if re.search(r"rss|atom", link.get("type", ""), re.I) and link.get("href"):
            url = urljoin(base_url, link["href"])
            if "comments" not in url:
                out.append(url)
    return out[:1]


def sitemap_urls(site_url: str) -> list[str]:
    """URLs from robots.txt sitemaps or /sitemap.xml (one level of sitemap index)."""
    sitemaps = robots_sitemaps(site_url) or [origin(site_url) + "/sitemap.xml"]
    urls: list[str] = []
    for sm in sitemaps[:2]:
        locs, is_index = _read_sitemap(sm)
        if is_index:
            # WordPress-style indexes: page sitemaps first, skip huge post/product ones.
            children = sorted(locs, key=lambda u: (0 if "page" in u else 1 if "post" not in u else 2))
            for child in children[:3]:
                urls += _read_sitemap(child)[0]
        else:
            urls += locs
        if len(urls) >= MAX_SITEMAP_URLS:
            break
    return urls[:MAX_SITEMAP_URLS]


def parse_sitemap(content: bytes) -> tuple[list[str], bool]:
    """(<loc> URLs, is_sitemap_index)."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return [], False
    locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
    return locs, root.tag.endswith("sitemapindex")


def _read_sitemap(url: str) -> tuple[list[str], bool]:
    fr = fetch(url)
    return parse_sitemap(fr.content) if fr.ok else ([], False)


def discover(html: str, home_url: str, sitemap: list[str] | None = None) -> list[Candidate]:
    """All scored candidates for this site, best first. The homepage itself is excluded."""
    site = base_domain(urlparse(home_url).netloc)
    home_norm = home_url.rstrip("/")
    seen: dict[str, Candidate] = {}

    def add(url: str, anchor: str, source: str) -> None:
        key = url.rstrip("/")
        if key == home_norm or not host_allowed(url, site):
            return
        score, cat = score_link(url, anchor)
        if score <= 0 and source != "feed":
            return
        if source == "feed":
            score, cat = max(score, 5.0), "news_feed"
        if key not in seen or score > seen[key].score:
            seen[key] = Candidate(url=url, anchor=anchor, score=score, category=cat, source=source)

    for url, anchor in homepage_links(html, home_url):
        add(url, anchor, "homepage")
    for url in feed_links(html, home_url):
        add(url, "RSS feed", "feed")
    for url in sitemap or []:
        add(url, "", "sitemap")
    return sorted(seen.values(), key=lambda c: -c.score)


def select(candidates: list[Candidate], max_pages: int) -> tuple[list[Candidate], list[Candidate]]:
    """Best page per category first (so we cover about/team/careers/...), then fill by score.
    Returns (selected, unfetched remainder for gap-fill)."""
    good = [c for c in candidates if c.score >= MIN_SCORE]
    selected: list[Candidate] = []
    for cat in [*CATEGORIES, "news_feed"]:
        best = next((c for c in good if c.category == cat), None)
        if best and len(selected) < max_pages:
            selected.append(best)
    for c in good:
        if len(selected) >= max_pages:
            break
        if c not in selected:
            selected.append(c)
    chosen = {c.url for c in selected}
    remainder = [c for c in candidates if c.url not in chosen]
    return selected, remainder
