"""LLM extraction: one structured-output call, a deterministic grounding check, and one
bounded gap-fill round. All calls go through `call_llm` (cache + cost tracking + debug dump).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from src.cost import CostTracker
from src.discover import Candidate
from src.parse import Document, fit_to_budget, load
from src.schema import (
    Contacts, Extraction, Financials, GapFillExtraction, Identity, LinkPick, NonprofitProfile,
    OpenRole, Person, Signals, Technology, Tool,
)

# Cheapest model per provider that holds up on extraction. Override with LLM_MODEL.
DEFAULT_MODELS = {"anthropic": "claude-haiku-4-5", "openai": "gpt-4.1-mini"}
CACHE_DIR = Path(".cache/llm")
GAP_FIELDS = ("leadership", "general_contact", "annual_revenue")
MAX_GAPFILL_URLS = 3

M = TypeVar("M", bound=BaseModel)

EXTRACT_SYSTEM = """You extract a structured profile of a nonprofit organisation from pages crawled from its website. The output feeds a sales and research database, so accuracy matters more than completeness.

Rules:
- Use only facts stated in the documents. No outside knowledge, no guessing. If the documents don't support a field, return null (or an empty list).
- source_url: copy the url attribute of the <document> the fact came from.
- confidence: high = stated explicitly; medium = stated but ambiguous, partial or possibly outdated; low = inferred from indirect wording.
- evidence: copy a short verbatim snippet (max 30 words) from that document. Never paraphrase it.
- Dates in ISO 8601 (YYYY-MM-DD; YYYY-MM or YYYY if that is all the page gives). Money as a plain number plus ISO 4217 currency code ("$12.5 million" -> 12500000, USD).
- annual_revenue: total revenue or income for one fiscal year. Not a single gift, campaign goal, program budget or cumulative total.
- registration_id: the number only, as printed (e.g. "12-3456789" or "207076"). If several are shown, the main one (US EIN, or the first charity number).
- leadership: named executives, senior staff and board members (max 15). When a person is listed under a board or trustees heading, their title must say so (e.g. "Board Chair", "Board Member", "Trustee"). Add email or LinkedIn only if shown next to that person.
- general_contact: the organisation's general email, phone and contact page.
- open_roles: current paid job openings only (no volunteer roles). open_rfps: current requests for proposals, tenders or procurement notices only.
- recent_news: up to 8 of the newest dated news, press or blog items with their dates. Do not filter by age.
- funders_and_partners: organisations named as funders, sponsors or partners (max 15).
- impact_metrics: up to 5 short quantified claims about the organisation's own work, e.g. "Served 12,000 meals in 2024". Not outcomes of its grantees or partners.
- cause_area: the single best fit from the allowed list. geography_served.scope: local (one city or county), regional (state, province or multi-county area), national or international.
- looks_like_nonprofit: false if the site is clearly a business, government body or personal site; say why in not_nonprofit_reason."""

PICK_SYSTEM = """You help a web crawler decide which pages to fetch next. You get the fields still missing from a nonprofit's profile and a list of unfetched links from its site. Choose at most 3 URLs most likely to contain those fields, copied exactly from the list. If none look promising, return an empty list."""

_clients: dict[str, anthropic.Anthropic | openai.OpenAI] = {}


def llm_provider() -> str:
    """anthropic | openai. LLM_PROVIDER wins; otherwise it's inferred from the key, so testing
    with your own key means pasting one line. OpenAI-compatible servers (Ollama, OpenRouter,
    vLLM) count as openai with LLM_BASE_URL set."""
    explicit = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    key = os.getenv("LLM_API_KEY") or ""
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key or os.getenv("LLM_BASE_URL") or (os.getenv("OPENAI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY")):
        return "openai"
    return "anthropic"


def llm_model() -> str:
    return os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(llm_provider(), "")


def _anthropic() -> anthropic.Anthropic:
    if "anthropic" not in _clients:
        # LLM_API_KEY if set; otherwise the SDK's own lookup (ANTHROPIC_API_KEY, profiles).
        _clients["anthropic"] = anthropic.Anthropic(api_key=os.getenv("LLM_API_KEY") or None)
    return _clients["anthropic"]


def _openai() -> openai.OpenAI:
    if "openai" not in _clients:
        base_url = os.getenv("LLM_BASE_URL") or None
        key = os.getenv("LLM_API_KEY") or ("not-needed" if base_url else None)  # local servers take any key
        _clients["openai"] = openai.OpenAI(api_key=key, base_url=base_url)
    return _clients["openai"]


def _complete_anthropic(model: str, system: str, messages: list[dict], schema: dict,
                        max_tokens: int) -> tuple[str, dict, str]:
    resp = _anthropic().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    u = resp.usage
    usage = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_input_tokens or 0,
        "cache_write_tokens": u.cache_creation_input_tokens or 0,
    }
    return "".join(b.text for b in resp.content if b.type == "text"), usage, resp.stop_reason or ""


def _complete_openai(model: str, system: str, messages: list[dict], schema: dict,
                     max_tokens: int, name: str) -> tuple[str, dict, str]:
    # Chat Completions rather than the Responses API: OpenAI-compatible servers implement it.
    # OpenAI caches long prompt prefixes automatically; there is no cache_control to set.
    limit = {"max_tokens": max_tokens} if os.getenv("LLM_BASE_URL") else {"max_completion_tokens": max_tokens}
    resp = _openai().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, *messages],
        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
        **limit,
    )
    choice, u = resp.choices[0], resp.usage
    cached = (u.prompt_tokens_details.cached_tokens or 0) if u and u.prompt_tokens_details else 0
    usage = {
        "input_tokens": (u.prompt_tokens - cached) if u else 0,
        "output_tokens": u.completion_tokens if u else 0,  # includes reasoning tokens, billed as output
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }
    stop = "refusal" if choice.message.refusal else {"length": "max_tokens"}.get(choice.finish_reason, choice.finish_reason or "")
    return choice.message.content or "", usage, stop


# ---------------------------------------------------------------- LLM call helper

def call_llm(
    system: str,
    user: str,
    model_cls: type[M],
    purpose: str,
    tracker: CostTracker,
    use_cache: bool = True,
    debug_dir: Path | None = None,
    max_tokens: int = 8000,
) -> tuple[M | None, list[str]]:
    """One structured-output call, validated against `model_cls`, retried once with the
    validation error. Returns (parsed or None, errors)."""
    provider, model = llm_provider(), llm_model()
    # Same strict schema for both providers: every object closed, every field required.
    schema = anthropic.transform_schema(model_cls.model_json_schema())
    messages: list[dict] = [{"role": "user", "content": user}]
    errors: list[str] = []
    text = ""
    for attempt in (1, 2):
        key = hashlib.sha256(json.dumps(
            [provider, os.getenv("LLM_BASE_URL") or None, model, system, messages, schema, max_tokens],
            sort_keys=True).encode()).hexdigest()
        cache_file = CACHE_DIR / f"{key}.json"
        if use_cache and cache_file.exists():
            cached = json.loads(cache_file.read_text())
            text = cached["text"]
            tracker.add(purpose, model, cached["usage"], cached_locally=True)
        else:
            try:
                if provider == "anthropic":
                    text, usage, stop = _complete_anthropic(model, system, messages, schema, max_tokens)
                elif provider == "openai":
                    text, usage, stop = _complete_openai(model, system, messages, schema, max_tokens, model_cls.__name__)
                else:
                    raise ValueError(f"unknown LLM_PROVIDER {provider!r}; use anthropic or openai")
            except (anthropic.AuthenticationError, openai.AuthenticationError):
                return None, errors + [f"{purpose}: {provider} authentication failed; check LLM_API_KEY in .env"]
            except (anthropic.APIError, openai.APIError) as e:
                return None, errors + [f"{purpose}: {provider} API error: {type(e).__name__}: {e}"]
            except Exception as e:  # noqa: BLE001 - e.g. no credentials configured at all
                return None, errors + [f"{purpose}: LLM call failed: {type(e).__name__}: {e}"]
            tracker.add(purpose, model, usage)
            if stop in ("max_tokens", "refusal"):
                errors.append(f"{purpose}: stop_reason={stop}")
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"text": text, "usage": usage}))
        if debug_dir and attempt == 1:
            _dump(debug_dir, purpose, system, user, text)
        try:
            return model_cls.model_validate_json(text), errors
        except ValidationError as e:
            errors.append(f"{purpose}: validation failed (attempt {attempt}): {_short(e)}")
            messages = messages + [
                {"role": "assistant", "content": text or "(empty)"},
                {"role": "user", "content": f"That JSON failed validation:\n{_short(e)}\n"
                                            "Return the full corrected JSON. Be concise if it was cut off."},
            ]
    if debug_dir:
        (debug_dir / f"{purpose}_response_retry.json").write_text(text)
    return None, errors


def _short(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:8])


def _dump(debug_dir: Path, purpose: str, system: str, user: str, text: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"{purpose}_input.txt").write_text(f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}")
    try:
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        pass
    (debug_dir / f"{purpose}_response.json").write_text(text)


def documents_block(docs: list[Document]) -> str:
    parts = [f'<document url="{d.url}" type="{d.kind}" page="{d.page_type}">\n{d.text}\n</document>'
             for d in docs]
    return "<documents>\n" + "\n".join(parts) + "\n</documents>"


# ---------------------------------------------------------------- extraction

def extract(docs: list[Document], tracker: CostTracker, use_cache: bool = True,
            debug_dir: Path | None = None) -> tuple[Extraction | None, list[str]]:
    docs, dropped = fit_to_budget(docs)
    user = documents_block(docs) + "\n\nExtract the organisation profile from these documents."
    ex, errors = call_llm(EXTRACT_SYSTEM, user, Extraction, "extract", tracker, use_cache, debug_dir)
    if dropped:
        errors.append(f"token budget: {len(dropped)} document(s) left out of the prompt: {', '.join(dropped)}")
    return ex, errors


# ---------------------------------------------------------------- grounding check

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class _Grounder:
    """Checks the LLM's claims against the text it was given. Cheap and catches most
    hallucinated names, emails and quotes."""

    def __init__(self, docs: list[Document]):
        self.text = {d.url.rstrip("/"): _norm(d.text) for d in docs}
        self.all = " ".join(self.text.values())
        self.digits = re.sub(r"\D", "", " ".join(d.text for d in docs))
        self.raw = " ".join((d.raw_html or "") + d.text for d in docs)

    def known(self, url: str | None) -> bool:
        return bool(url) and url.rstrip("/") in self.text

    def found(self, snippet: str, url: str | None = None) -> bool:
        n = _norm(snippet)
        if not n:
            return True
        hay = self.text.get((url or "").rstrip("/"), self.all)
        if n in hay or n in self.all:
            return True
        # Small wording drift (ellipses, dropped words) is fine; invented quotes aren't.
        toks = n.split()
        vocab = set(self.all.split())
        return len(toks) >= 4 and sum(t in vocab for t in toks) / len(toks) >= 0.8

    def url_seen(self, url: str) -> bool:
        """A URL is real if we fetched it or a page links to / mentions it."""
        if self.known(url) or url in self.raw:
            return True
        path = urlparse(url).path.rstrip("/")
        return not path or f'{path}"' in self.raw or f"{path}/" in self.raw or f"{path}'" in self.raw

    def phone_found(self, phone: str) -> bool:
        digits = re.sub(r"\D", "", phone)
        return len(digits) >= 7 and digits[-7:] in self.digits


def _items_with_source(obj: BaseModel):
    for name in type(obj).model_fields:
        val = getattr(obj, name)
        for item in val if isinstance(val, list) else [val]:
            if isinstance(item, BaseModel) and hasattr(item, "source_url"):
                yield name, item


def ground_check(ex: Extraction | GapFillExtraction, docs: list[Document]) -> list[str]:
    """Downgrade or drop claims the documents don't support. Mutates `ex`; returns notes."""
    g = _Grounder(docs)
    notes: list[str] = []
    for field, item in _items_with_source(ex):
        if not g.known(item.source_url):
            notes.append(f"grounding: {field} cites unknown source {item.source_url!r}")
            if hasattr(item, "confidence"):
                item.confidence = "low"
        evidence = getattr(item, "evidence", None)
        if evidence and not g.found(evidence, item.source_url):
            notes.append(f"grounding: {field} evidence not found in source, confidence set to low")
            item.confidence = "low"
        link = getattr(item, "url", None)
        if link and not g.url_seen(link):
            notes.append(f"grounding: removed {field} url {link!r} (not linked from any page)")
            item.url = None

    kept = []
    for p in ex.leadership:
        if not g.found(p.name):
            notes.append(f"grounding: dropped leader {p.name!r} (name not in any document)")
            continue
        if p.email and not g.found(p.email):
            notes.append(f"grounding: removed unverified email for {p.name!r}")
            p.email = None
        kept.append(p)
    ex.leadership = kept

    gc = ex.general_contact
    if gc:
        if gc.email and not g.found(gc.email):
            notes.append(f"grounding: removed unverified general email {gc.email!r}")
            gc.email = None
        if gc.phone and not g.phone_found(gc.phone):
            notes.append(f"grounding: removed unverified phone {gc.phone!r}")
            gc.phone = None
        if gc.contact_page_url and not g.url_seen(gc.contact_page_url):
            notes.append(f"grounding: removed contact page {gc.contact_page_url!r} (not linked from any page)")
            gc.contact_page_url = None

    reg = getattr(ex, "registration_id", None)
    if reg and not g.found(reg.value):
        notes.append("grounding: registration_id not found verbatim, confidence set to low")
        reg.confidence = "low"
    return notes


# ---------------------------------------------------------------- profile assembly

def build_profile(ex: Extraction | None, *, input_: str, resolved_url: str | None,
                  now: datetime, tools: list[Tool]) -> NonprofitProfile:
    p = NonprofitProfile(input=input_, resolved_url=resolved_url, extracted_at=now, status="partial")
    p.technology = Technology(last_checked=now, tech_stack=tools)
    if ex is None:
        p.identity = Identity(website=resolved_url)
        return p
    p.identity = Identity(
        last_checked=now, name=ex.name, legal_name=ex.legal_name, website=resolved_url,
        registration_id=ex.registration_id, country=ex.country, hq_location=ex.hq_location,
        mission=ex.mission, programs=ex.programs, cause_area=ex.cause_area,
        geography_served=ex.geography_served, impact_metrics=ex.impact_metrics,
        looks_like_nonprofit=ex.looks_like_nonprofit,
    )
    p.financials = Financials(last_checked=now, annual_revenue=ex.annual_revenue,
                              staff_count=ex.staff_count, tax_status=ex.tax_status)
    p.contacts = Contacts(last_checked=now, general_contact=ex.general_contact,
                          leadership=[Person(**x.model_dump()) for x in ex.leadership])
    p.signals = Signals(
        last_checked=now, open_rfps=ex.open_rfps, recent_news=ex.recent_news,
        funders_and_partners=ex.funders_and_partners,
        open_roles=[OpenRole(**r.model_dump()) for r in ex.open_roles],
    )
    if not ex.looks_like_nonprofit:
        p.flags.append("not_a_nonprofit?")
        if ex.not_nonprofit_reason:
            p.errors.append(f"site may not be a nonprofit: {ex.not_nonprofit_reason}")
    return p


# ---------------------------------------------------------------- gap-fill (bounded agentic step)

def missing_fields(p: NonprofitProfile) -> list[str]:
    missing = []
    if not p.contacts.leadership:
        missing.append("leadership")
    gc = p.contacts.general_contact
    if gc is None or not (gc.email or gc.phone):
        missing.append("general_contact")
    if p.financials.annual_revenue is None:
        missing.append("annual_revenue")
    return missing


def gap_fill(p: NonprofitProfile, remainder: list[Candidate], tracker: CostTracker,
             use_cache: bool = True, debug_dir: Path | None = None) -> list[Document]:
    """If key fields are empty, let the LLM pick <=3 unfetched links, fetch them and
    re-extract only the missing fields. One round, max 2 LLM calls. Returns new docs."""
    missing = missing_fields(p)
    p.gapfill.missing = missing
    if not missing:
        p.gapfill.note = "not needed"
        return []
    crawled = {pg.url.rstrip("/") for pg in p.pages_crawled}
    options = [c for c in remainder if c.url.rstrip("/") not in crawled][:40]
    if not options:
        p.gapfill.note = "key fields missing but no unfetched links left"
        return []

    p.gapfill.triggered = True
    listing = "\n".join(f"- {c.url} | {c.anchor or '-'} | {c.category}" for c in options)
    pick, errors = call_llm(
        PICK_SYSTEM, f"Missing fields: {', '.join(missing)}\n\nUnfetched links (url | anchor | guess):\n{listing}",
        LinkPick, "gapfill_pick", tracker, use_cache, debug_dir, max_tokens=500)
    p.errors += errors
    allowed = {c.url: c for c in options}
    urls = [u for u in (pick.urls if pick else []) if u in allowed][:MAX_GAPFILL_URLS]
    p.gapfill.urls = urls
    if not urls:
        p.gapfill.note = "LLM found no promising links"
        return []

    docs: list[Document] = []
    for url in urls:
        doc, rec = load(url, allowed[url].category, via="gapfill")
        if doc:
            docs.append(doc)
        p.pages_crawled.append(rec)
    if not docs:
        p.gapfill.note = "picked links could not be fetched"
        return []

    user = (documents_block(docs) + f"\n\nExtract ONLY these fields: {', '.join(missing)}. "
            "Leave the other fields null or empty.")
    sub, errors = call_llm(EXTRACT_SYSTEM, user, GapFillExtraction, "gapfill_extract", tracker, use_cache, debug_dir)
    p.errors += errors
    if sub is None:
        p.gapfill.note = "re-extraction failed"
        return docs
    p.errors += ground_check(sub, docs)

    if "leadership" in missing and sub.leadership:
        p.contacts.leadership = [Person(**x.model_dump()) for x in sub.leadership]
        p.gapfill.filled.append("leadership")
    if "general_contact" in missing and sub.general_contact and (sub.general_contact.email or sub.general_contact.phone):
        p.contacts.general_contact = sub.general_contact
        p.gapfill.filled.append("general_contact")
    if "annual_revenue" in missing and sub.annual_revenue:
        p.financials.annual_revenue = sub.annual_revenue
        p.gapfill.filled.append("annual_revenue")
    p.gapfill.note = f"filled {len(p.gapfill.filled)}/{len(missing)}"
    return docs
