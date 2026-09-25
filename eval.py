"""Score pipeline outputs (output/<slug>.json) against hand-labelled gold data (tests/gold/).

  python eval.py

Per field: correct (right value, or correctly empty), wrong (filled but incorrect: a
hallucination or misread; target ~0), missed (empty although the site has it).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from src.categorise import FX_TO_USD

GOLD_DIR = Path("tests/gold")
OUT_DIR = Path("output")
TECH_CATEGORIES = ("donor_crm", "donation_platform")


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _v(sourced):
    return sourced["value"] if sourced else None


def extracted(field: str, profile: dict):
    ident, fin, con = profile["identity"], profile["financials"], profile["contacts"]
    if field == "name":
        return _v(ident["name"])
    if field == "registration_id":
        return _v(ident["registration_id"])
    if field == "annual_revenue_usd":
        rev = _v(fin["annual_revenue"])
        rate = FX_TO_USD.get(rev["currency"]) if rev else None
        return rev["amount"] * rate if rev and rate else None
    if field == "executive_name":
        return next((p["name"] for p in con["leadership"] if p["buyer_role"] == "executive"), None)
    if field == "contact_email":
        return (con["general_contact"] or {}).get("email")
    if field == "contact_phone":
        return (con["general_contact"] or {}).get("phone")
    if field == "has_careers_page":
        return any(p["page_type"] == "careers" and not p["error"] for p in profile["pages_crawled"])
    if field == "open_roles_count":
        return len(profile["signals"]["open_roles"])
    if field == "fundraising_tech":
        return sorted(t["tool"] for t in profile["technology"]["tech_stack"] if t["category"] in TECH_CATEGORIES)
    if field == "cause_area":
        return _v(ident["cause_area"])
    if field == "country":
        return _v(ident["country"])
    if field == "hq_city":
        loc = _v(ident["hq_location"])
        return loc.split(",")[0].strip() if loc else None
    raise KeyError(field)


def judge(field: str, gold, got) -> str:
    if field == "has_careers_page":
        return "correct" if got == gold else ("missed" if gold else "wrong")
    if field == "open_roles_count":
        return "correct" if got == gold else ("wrong" if got > gold else "missed")
    if field == "fundraising_tech":
        g, e = set(gold), set(got)
        return "correct" if g == e else ("wrong" if e - g else "missed")
    if gold is None:
        return "correct" if got in (None, "", []) else "wrong"
    if got in (None, ""):
        return "missed"
    if field == "annual_revenue_usd":
        return "correct" if abs(got - gold["value"]) / gold["value"] <= gold["tolerance"] else "wrong"
    if field == "registration_id":
        return "correct" if re.sub(r"[^A-Z0-9]", "", got.upper()) in {re.sub(r"[^A-Z0-9]", "", x.upper()) for x in gold} else "wrong"
    if field == "contact_phone":
        return "correct" if re.sub(r"\D", "", got)[-10:] in {re.sub(r"\D", "", x)[-10:] for x in gold} else "wrong"
    if field == "executive_name":
        # Every part of the gold name must be there; middle initials may be extra.
        # "Jonathan T.M. Reckford" matches "Jonathan Reckford"; a bare "Scott" doesn't match "Scott Harrison".
        have = set(norm(got).split())
        return "correct" if any(set(norm(x).split()) <= have for x in gold) else "wrong"
    if field in ("name", "hq_city"):
        n = norm(got)
        return "correct" if any(norm(x) in n or n in norm(x) for x in gold) else "wrong"
    return "correct" if norm(got) in {norm(x) for x in gold} else "wrong"


def main() -> None:
    per_field: dict[str, Counter] = defaultdict(Counter)
    problems: list[str] = []
    for gold_file in sorted(GOLD_DIR.glob("*.json")):
        gold = json.loads(gold_file.read_text())
        out = OUT_DIR / gold_file.name
        if not out.exists():
            problems.append(f"- {gold_file.stem}: no output yet (run: python run.py {gold['website']})")
            continue
        profile = json.loads(out.read_text())["profile"]
        for field, expected in gold["fields"].items():
            got = extracted(field, profile)
            verdict = judge(field, expected, got)
            per_field[field][verdict] += 1
            if verdict != "correct":
                problems.append(f"- {gold_file.stem} / {field}: **{verdict}**, got {got!r}, gold {expected!r}")

    total = Counter()
    print("| field | correct | wrong | missed |\n|---|---|---|---|")
    for field, c in per_field.items():
        total.update(c)
        print(f"| {field} | {c['correct']} | {c['wrong']} | {c['missed']} |")
    n = sum(total.values())
    if n:
        print(f"| **overall** | **{total['correct']}/{n} ({total['correct'] / n:.0%})** "
              f"| **{total['wrong']}** | **{total['missed']}** |")
    if problems:
        print("\nNot correct:\n" + "\n".join(problems))


if __name__ == "__main__":
    main()
