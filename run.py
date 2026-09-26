"""Nonprofit profile agent CLI.

  python run.py "charity: water"                 # name (needs SEARCH_API_KEY)
  python run.py https://www.example.org          # URL
  python run.py --batch inputs.txt               # one input per line, '#' comments
  python run.py -v https://www.example.org       # stage-by-stage trace
  python run.py --spend                          # total LLM spend so far
"""
from __future__ import annotations

import argparse
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from src import categorise, discover, extract, output, techstack, views  # noqa: E402
from src.cost import CostTracker, log_run, total_spend  # noqa: E402
from src.parse import Document, is_near_empty, load, parse  # noqa: E402
from src.resolve import merge_registry, propublica_lookup, resolve_input, us_city, us_state  # noqa: E402
from src.schema import Extraction, NonprofitProfile, PageRecord, Status  # noqa: E402

R = TypeVar("R")


class Trace:
    """-v output: one compact block per stage. Stage modules never print."""

    def __init__(self, on: bool):
        self.on = on

    def __call__(self, stage: str, *lines: str) -> None:
        if self.on:
            print(f"\n[{stage}]")
            for line in lines:
                print(f"  {line}")


def safe(stage: str, fn: Callable[[], R], default: R, errors: list[str]) -> R:
    """Run one stage; on an unexpected exception log it and carry on with `default`."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - fail soft
        errors.append(f"{stage} failed: {type(e).__name__}: {e}")
        return default


def filled_fields(ex: Extraction) -> tuple[list[str], list[str]]:
    filled, empty = [], []
    for name in Extraction.model_fields:
        if name in ("looks_like_nonprofit", "not_nonprofit_reason"):
            continue
        (filled if getattr(ex, name) not in (None, []) else empty).append(name)
    return filled, empty


def compute_status(p: NonprofitProfile, extracted: bool, js_empty: bool) -> Status:
    """ok = usable for outreach: extraction worked, looks like a nonprofit, a contact route exists,
    >=6 of 8 core fields."""
    ident, fin, con = p.identity, p.financials, p.contacts
    core = [ident.name, ident.mission, ident.cause_area, ident.programs, ident.hq_location,
            con.leadership, con.general_contact, fin.annual_revenue]
    has_contact = bool(con.leadership or con.general_contact)
    if not extracted or js_empty or not has_contact or ident.looks_like_nonprofit is False \
            or sum(bool(x) for x in core) < 6:
        return "partial"
    return "ok"


def foreign_tld(url: str) -> bool:
    tld = urlparse(url).netloc.rsplit(".", 1)[-1]
    return len(tld) == 2 and tld not in {"us", "io", "co", "ai"}


def profile_org(raw: str, *, use_cache: bool = True, max_pages: int = 10,
                verbose: bool = False) -> tuple[NonprofitProfile, Path, dict]:
    trace = Trace(verbose)
    now = datetime.now(timezone.utc)
    tracker = CostTracker()

    # 1. resolve ----------------------------------------------------------------
    res = resolve_input(raw)
    trace("resolve", f"input: {raw!r}", f"method: {res.method}  url: {res.url}", *res.notes,
          *([f"ERROR: {res.error}"] if res.error else []))
    if res.error:
        p = NonprofitProfile(input=raw, resolved_url=res.url, extracted_at=now, status="failed", errors=[res.error])
        if res.homepage:
            p.pages_crawled.append(PageRecord(url=res.homepage.url, page_type="homepage",
                                              status=res.homepage.status, error=res.homepage.error))
        return finish(p, trace, tracker)

    errors: list[str] = []
    pages: list[PageRecord] = []
    docs: list[Document] = []
    try:
        home = parse(res.homepage, "homepage")
    except ValueError as e:
        p = NonprofitProfile(input=raw, resolved_url=res.url, extracted_at=now, status="failed",
                             errors=[f"homepage could not be parsed: {e}"])
        return finish(p, trace, tracker)
    docs.append(home)
    pages.append(PageRecord(url=home.url, page_type="homepage", kind=home.kind,
                            status=res.homepage.status, tokens=home.est_tokens))
    js_empty = home.kind == "html" and is_near_empty(home.text)
    if js_empty:
        errors.append("homepage has almost no visible text (likely rendered by JavaScript); "
                      "results will be thin. Headless rendering is a listed future improvement.")
    ddir = output.debug_dir(res.url, raw)

    # 2. discover ---------------------------------------------------------------
    sitemap = safe("sitemap", lambda: discover.sitemap_urls(res.url), [], errors)
    candidates = safe("discover", lambda: discover.discover(home.raw_html or "", res.url, sitemap), [], errors)
    selected, remainder = discover.select(candidates, max_pages - 1)
    output.write_json(ddir / "candidates.json", [c.model_dump() for c in candidates])
    trace("discover", f"{len(sitemap)} sitemap URLs, {len(candidates)} scored candidates, {len(selected)} selected",
          *[f"{c.score:5.1f}  {c.category:10} {c.url}" for c in selected])

    # 3. fetch & parse ------------------------------------------------------------
    def load_into(url: str, page_type: str) -> None:
        doc, rec = load(url, page_type)
        if doc:
            docs.append(doc)
        if rec.error:
            errors.append(f"page {url}: {rec.error}")
        pages.append(rec)

    for c in selected:
        load_into(c.url, c.category)
    # One hop: annual reports are usually PDFs linked from the financials page, not the homepage.
    fin = next((d for d in docs if d.page_type == "financials" and d.raw_html), None)
    if fin and not any(d.kind == "pdf" for d in docs):
        hop = safe("pdf hop", lambda: discover.pdf_links(fin.raw_html, fin.url), [], errors)
        if hop:
            load_into(hop[0].url, "financials_pdf")
    trace("fetch", *[f"{r.status or '---'}  {r.kind or '-':4} {r.tokens:6} tok  {r.page_type:10} {r.url}"
                     + (f"   ! {r.error}" if r.error else "") for r in pages])

    # 4. tech stack (deterministic) ---------------------------------------------
    tools = safe("techstack", lambda: techstack.detect(docs), [], errors)
    trace("techstack", *([f"{t.tool} ({t.category})  <- {t.matched[:70]}" for t in tools] or ["none detected"]))

    # 5. extract + grounding ------------------------------------------------------
    ex, ex_errors = extract.extract(docs, tracker, use_cache, ddir)
    notes = extract.ground_check(ex, docs) if ex else []
    p = extract.build_profile(ex, input_=raw, resolved_url=res.url, now=now, tools=tools)
    p.pages_crawled = pages
    p.errors = errors + ex_errors + notes + p.errors
    if ex:
        filled, empty = filled_fields(ex)
        call = tracker.calls[-1]
        trace("extract", f"model: {extract.llm_provider()} / {call.model}",
              f"tokens in/out: {call.input_tokens + call.cache_read_tokens}/{call.output_tokens}"
              f"{'  (local cache)' if call.cached_locally else ''}",
              f"filled ({len(filled)}): {', '.join(filled)}", f"empty ({len(empty)}): {', '.join(empty)}",
              *ex_errors, *notes)
    else:
        trace("extract", "FAILED", *ex_errors)

    # 6. registry (US: IRS 990 data via ProPublica) ------------------------------
    country = p.identity.country.value if p.identity.country else None
    if ex and country in (None, "US") and not foreign_tld(res.url):
        name = p.identity.legal_name or p.identity.name
        hq = p.identity.hq_location.value if p.identity.hq_location else None
        reg, note = safe("registry", lambda: propublica_lookup(
            name.value if name else None,
            p.identity.registration_id.value if p.identity.registration_id else None,
            us_state(hq), us_city(hq)), (None, "lookup crashed"), p.errors)
        if reg:
            merge_registry(p, reg, categorise.ntee_to_cause_area)
        else:
            p.errors.append(f"registry: {note}")
    else:
        note = "skipped (no extraction)" if not ex else f"skipped (country={country})"
    trace("registry", note)

    # 7. gap-fill (bounded agentic step) -------------------------------------------
    if not ex:
        p.gapfill.note = "skipped (no extraction)"
    else:
        new_docs = safe("gap-fill", lambda: extract.gap_fill(p, remainder, tracker, use_cache, ddir), [], p.errors)
        known = {t.tool for t in p.technology.tech_stack}
        p.technology.tech_stack += [t for t in techstack.detect(new_docs) if t.tool not in known]
    g = p.gapfill
    trace("gap-fill", f"missing after first pass: {g.missing or 'none'}", f"triggered: {g.triggered}",
          *([f"urls: {', '.join(g.urls)}"] if g.urls else []), f"filled: {g.filled or '-'}  ({g.note})")

    # 8. rules + status -------------------------------------------------------------
    categorise.apply_rules(p, now.date())
    p.status = compute_status(p, ex is not None, js_empty)
    return finish(p, trace, tracker)


def finish(p: NonprofitProfile, trace: Trace, tracker: CostTracker) -> tuple[NonprofitProfile, Path, dict]:
    today = p.extracted_at.date()
    p.cost = tracker.summary()
    sales, funder = views.sales_view(p, today), views.funder_view(p)
    path = output.write_profile(p, {"sales": sales, "funder": funder})
    csv_path = output.upsert_csv(views.to_csv_row(p, sales, str(path)))
    log_run(p.input, p.cost)
    trace("views", f"status: {p.status}   lead_score: {sales['lead_score']}",
          *[f"{i['points']:+d}  {i['signal']}: {i['detail']}" for i in sales["score_breakdown"]],
          f"why_now: {sales['why_now']}")
    trace("output", str(path), str(csv_path), f"errors logged: {len(p.errors)}")
    trace("cost", *[f"{c.purpose:16} in {c.input_tokens + c.cache_read_tokens:6}  out {c.output_tokens:5}  "
                    f"{_usd(c.usd_list)}{'  (cached: $0)' if c.cached_locally else ''}" for c in p.cost.calls],
          f"total: ${p.cost.usd:.4f} spent  (${p.cost.usd_list:.4f} uncached)"
          + ("  [some prices unknown: set LLM_PRICE_IN/LLM_PRICE_OUT]" if any(c.usd_list is None for c in p.cost.calls) else ""))
    return p, path, sales


def _usd(value: float | None) -> str:
    return "$?" if value is None else f"${value:.4f}"


def run_batch(path: str, **kw) -> None:
    lines = [re.sub(r"\s+#.*$", "", ln).strip() for ln in Path(path).read_text().splitlines()]
    inputs = [ln for ln in lines if ln and not ln.startswith("#")]
    print(f"LLM: {extract.llm_provider()} / {extract.llm_model()}   inputs: {len(inputs)}")
    rows = []
    for i, item in enumerate(inputs, 1):
        print(f"[{i}/{len(inputs)}] {item}", flush=True)
        try:
            p, out, sales = profile_org(item, **kw)
            rows.append((item, p.status, str(sales["lead_score"]), str(len(p.pages_crawled)),
                         _usd(p.cost.usd_list), out.name))
        except Exception as e:  # noqa: BLE001 - one bad org must not stop the batch
            traceback.print_exc()
            rows.append((item, "crashed", "-", "-", "-", f"{type(e).__name__}: {e}"))
    print("\n| input | status | score | pages | llm cost | output |\n|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a structured nonprofit profile from a name or URL.")
    ap.add_argument("input", nargs="?", help="organisation name or website URL")
    ap.add_argument("--batch", help="file with one name/URL per line")
    ap.add_argument("-v", "--verbose", action="store_true", help="print a stage-by-stage trace")
    ap.add_argument("--no-cache", action="store_true", help="ignore the local LLM response cache")
    ap.add_argument("--max-pages", type=int, default=10, help="pages/files to fetch per org (incl. homepage)")
    ap.add_argument("--spend", action="store_true", help="print total LLM spend from output/cost_log.jsonl")
    args = ap.parse_args()

    if args.spend:
        print(total_spend())
        return
    kw = {"use_cache": not args.no_cache, "max_pages": args.max_pages, "verbose": args.verbose}
    if args.batch:
        run_batch(args.batch, **kw)
    elif args.input:
        p, path, sales = profile_org(args.input, **kw)
        print(f"{p.status}: {path}  (lead_score {sales['lead_score']}, ${p.cost.usd:.4f})")
        if p.status == "failed":
            print(f"  reason: {p.errors[0]}")
            sys.exit(1)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
