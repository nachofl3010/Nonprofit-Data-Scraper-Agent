"""Deterministic tech-stack detection: match script/iframe/form/link URLs in raw HTML
against a fingerprint dictionary. No LLM cost, fully reproducible.

We scan tag attributes and inline scripts only, never visible text, so a news post that
*mentions* Salesforce doesn't count as Salesforce being in the stack. Patterns target
product hosts (e.g. force.com, secure.etapestry.com) rather than vendor marketing domains
for the same reason.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.parse import Document
from src.schema import Tool

# tool -> (category, patterns). Categories: donor_crm, donation_platform, email_marketing,
# marketing_automation, events, recruiting, cms.
FINGERPRINTS: dict[str, tuple[str, list[str]]] = {
    # donor CRMs / fundraising suites: the primary user's competitors or migration targets
    "Blackbaud Luminate Online": ("donor_crm", [r"luminateonline\.com", r"convio\.(net|com)", r"/site/Donation2"]),
    "Blackbaud eTapestry": ("donor_crm", [r"etapestry\.com"]),
    "Blackbaud (Raiser's Edge NXT / Online Express)": (
        "donor_crm", [r"bbox\.blackbaud", r"nxt\.blackbaud", r"sky\.blackbaud", r"blackbaudhosting\.com",
                      r"bbcdn\.com", r"blackbaud-sites", r"sites\.blackbaud", r"secure\.blackbaud"]),
    "Salesforce": ("donor_crm", [r"\.force\.com", r"salesforce-sites\.com", r"salesforceliveagent", r"\.my\.site\.com"]),
    "Bloomerang": ("donor_crm", [r"bloomerang\.co", r"bloomerang-"]),
    "Neon CRM": ("donor_crm", [r"neoncrm\.com", r"neonone\.com"]),
    "DonorPerfect": ("donor_crm", [r"donorperfect\.(net|com)"]),
    "Virtuous": ("donor_crm", [r"virtuoussoftware\.com", r"virtuous\.org"]),
    "EveryAction / Bonterra": ("donor_crm", [r"everyaction\.com", r"ngpvan\.com", r"actionkit\.com"]),
    "Little Green Light": ("donor_crm", [r"littlegreenlight\.com"]),
    "Kindful": ("donor_crm", [r"kindful\.com"]),
    "Beacon CRM": ("donor_crm", [r"beaconcrm\.org", r"beaconforms"]),
    "Donorfy": ("donor_crm", [r"donorfy\.com"]),
    # online giving
    "Classy / GoFundMe Pro": ("donation_platform", [r"classy\.org", r"classy-cdn", r"gofundme\.com/pro"]),
    "Givebutter": ("donation_platform", [r"givebutter\.com"]),
    "Donorbox": ("donation_platform", [r"donorbox\.org"]),
    "Qgiv": ("donation_platform", [r"qgiv\.com"]),
    "Fundraise Up": ("donation_platform", [r"fundraiseup\.com"]),
    "Give Lively": ("donation_platform", [r"givelively\.org"]),
    "Network for Good": ("donation_platform", [r"networkforgood\.(com|org)"]),
    "MobileCause": ("donation_platform", [r"mobilecause\.com"]),
    "Funraise": ("donation_platform", [r"funraise\.(io|org)"]),
    "Donately": ("donation_platform", [r"donately\.com"]),
    "JustGiving": ("donation_platform", [r"justgiving\.com"]),
    "Enthuse": ("donation_platform", [r"enthuse\.com"]),
    "PayPal Giving": ("donation_platform", [r"paypal\.com/donate", r"paypal\.com/fundraiser"]),
    # email / marketing
    "Mailchimp": ("email_marketing", [r"list-manage\.com", r"chimpstatic\.com", r"mailchi\.mp"]),
    "Constant Contact": ("email_marketing", [r"constantcontact\.com", r"ctctcdn\.com"]),
    "Campaign Monitor": ("email_marketing", [r"createsend\.com", r"cmail\d+\.com"]),
    "HubSpot": ("marketing_automation", [r"hs-scripts\.com", r"hsforms\.(net|com)", r"hs-analytics\.net",
                                         r"hubspot\.com/"]),
    "Salesforce Pardot": ("marketing_automation", [r"pardot\.com", r"pi\.pardot"]),
    # events
    "Eventbrite": ("events", [r"eventbrite\.[a-z.]+"]),
    "GiveSmart": ("events", [r"givesmart\.com"]),
    "OneCause": ("events", [r"onecause\.com"]),
    "Greater Giving": ("events", [r"greatergiving\.com"]),
    # recruiting
    "Greenhouse": ("recruiting", [r"greenhouse\.io"]),
    "Lever": ("recruiting", [r"lever\.co\b"]),
    "Workable": ("recruiting", [r"workable\.com"]),
    "BambooHR": ("recruiting", [r"bamboohr\.com"]),
    "JazzHR": ("recruiting", [r"applytojob\.com"]),
    "Paylocity": ("recruiting", [r"paylocity\.com"]),
    "ADP Workforce Now": ("recruiting", [r"workforcenow\.adp\.com"]),
    # website platform (a rough proxy for org size / web maturity)
    "WordPress": ("cms", [r"/wp-content/", r"/wp-includes/"]),
    "Squarespace": ("cms", [r"squarespace\.com", r"squarespace-cdn"]),
    "Wix": ("cms", [r"wixstatic\.com", r"parastorage\.com"]),
    "Webflow": ("cms", [r"webflow\.(com|io)", r"website-files\.com"]),
    "Drupal": ("cms", [r"/sites/default/files/", r"drupal-settings-json"]),
}
_compiled = {tool: (cat, [re.compile(p, re.I) for p in pats]) for tool, (cat, pats) in FINGERPRINTS.items()}


def haystack(html: str) -> list[tuple[bool, str]]:
    """(is_outbound_link, value) for attribute URLs and inline script bodies: where embeds
    and widgets reveal themselves."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[bool, str]] = []
    for tag, attr in (("script", "src"), ("iframe", "src"), ("form", "action"), ("link", "href"),
                      ("a", "href"), ("img", "src"), ("meta", "content")):
        for el in soup.find_all(tag):
            val = el.get(attr)
            if val:
                out.append((tag == "a", val if isinstance(val, str) else " ".join(val)))
    out += [(False, s.string) for s in soup.find_all("script") if s.string]
    for el in soup.find_all(attrs={"data-settings": True}):  # some embeds hide config here
        out.append((False, el["data-settings"]))
    return out


def detect_html(html: str, source_url: str) -> list[Tool]:
    found: list[Tool] = []
    items = haystack(html)
    for tool, (cat, patterns) in _compiled.items():
        for is_link, item in items:
            if is_link and cat == "cms":
                continue  # linking to a Drupal site doesn't make this site Drupal
            m = next((p.search(item) for p in patterns if p.search(item)), None)
            if m:
                snippet = item if len(item) <= 120 else item[max(0, m.start() - 40): m.end() + 40]
                found.append(Tool(tool=tool, category=cat, source_url=source_url, matched=snippet.strip()))
                break
    return found


def detect(docs: list[Document]) -> list[Tool]:
    """Tools across all fetched HTML pages, one entry per tool (first page where seen)."""
    seen: dict[str, Tool] = {}
    for d in docs:
        if d.raw_html:
            for t in detect_html(d.raw_html, d.url):
                seen.setdefault(t.tool, t)
    order = ["donor_crm", "donation_platform", "marketing_automation", "email_marketing", "events", "recruiting", "cms"]
    return sorted(seen.values(), key=lambda t: order.index(t.category) if t.category in order else 99)
