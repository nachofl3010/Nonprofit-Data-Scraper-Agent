"""Core nonprofit profile schema.

Two layers:
- LLM-facing models (`Extraction`, `GapFillExtraction`, `LinkPick`) hold only the fields
  the LLM should fill. Smaller schema = fewer output tokens, and the LLM can't touch
  fields that code derives.
- `NonprofitProfile` is the final record: LLM fields + code-derived fields
  (size_band, buyer_role, is_buying_signal, tech_stack, financial_history) + run metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"
# Bump when the category lists below change, so old profiles can be re-mapped.
TAXONOMY_VERSION = "ntee-major-v1"

Confidence = Literal["high", "medium", "low"]
# NTEE major groups, collapsed to 14 buckets (see categorise.NTEE_TO_CAUSE).
CauseArea = Literal[
    "arts_culture", "education", "environment", "animals", "health", "mental_health",
    "human_services", "international", "civil_rights", "community_development",
    "philanthropy", "science_research", "religion", "other",
]
GeoScope = Literal["local", "regional", "national", "international"]
SizeBand = Literal["<$500k", "$500k-$5M", "$5M-$50M", ">$50M", "unknown"]
BuyerRole = Literal["executive", "fundraising", "finance", "operations_it", "board", "other"]
FunderType = Literal["foundation", "corporate", "government", "individual", "other"]
Status = Literal["ok", "partial", "failed"]
DocKind = Literal["html", "pdf", "csv", "xml"]

T = TypeVar("T")


# ---------------------------------------------------------------- provenance

class Sourced(BaseModel, Generic[T]):
    value: T
    source_url: str = Field(description="Exact url of the <document> this came from")
    confidence: Confidence


class SourcedCauseArea(Sourced[CauseArea]):
    """Named subclass so the JSON schema gets a short definition name."""


class Evidenced(Sourced[T], Generic[T]):
    """Provenance + verbatim evidence. Only for high-stakes fields a human would verify."""
    evidence: str | None = Field(description="Verbatim snippet from the document, max 30 words")


# ---------------------------------------------------------------- item models

class Program(BaseModel):
    name: str
    description: str = Field(description="One line")


class Geography(BaseModel):
    scope: GeoScope
    places: list[str] = Field(description="Cities/regions/countries served, as named on the site")


class Revenue(BaseModel):
    amount: float = Field(description="Plain number, e.g. 12500000 for '$12.5 million'")
    currency: str = Field(description="ISO 4217 code, e.g. USD, GBP")
    fiscal_year: int | None = Field(description="Fiscal year the figure refers to")


class LLMPerson(BaseModel):
    name: str
    title: str | None
    email: str | None = Field(description="Only if shown next to this person")
    linkedin: str | None
    source_url: str
    confidence: Confidence
    evidence: str | None = Field(description="Verbatim snippet naming the person and title")


class GeneralContact(BaseModel):
    email: str | None
    phone: str | None
    contact_page_url: str | None
    source_url: str


class LLMOpenRole(BaseModel):
    title: str
    url: str | None
    source_url: str
    confidence: Confidence
    evidence: str | None


class RFP(BaseModel):
    title: str
    due_date: str | None = Field(description="ISO 8601")
    url: str | None
    source_url: str
    confidence: Confidence
    evidence: str | None


class NewsItem(BaseModel):
    headline: str
    date: str | None = Field(description="ISO 8601 as shown on the page (YYYY-MM-DD, YYYY-MM or YYYY)")
    url: str | None
    summary: str | None = Field(description="One line")
    source_url: str


class FunderPartner(BaseModel):
    name: str
    type: FunderType
    source_url: str


# ---------------------------------------------------------------- LLM targets

class Extraction(BaseModel):
    """What the LLM fills in the single extraction call."""
    name: Sourced[str] | None
    legal_name: Sourced[str] | None
    registration_id: Evidenced[str] | None = Field(
        description="Number only: EIN (US, XX-XXXXXXX) or the main charity/registration number")
    country: Sourced[str] | None = Field(description="ISO 3166-1 alpha-2 code of HQ country")
    hq_location: Sourced[str] | None = Field(
        description="'City, ST' for US (2-letter state), 'City, Region' elsewhere")
    mission: Sourced[str] | None = Field(description="1-2 sentences")
    programs: list[Program] = Field(description="Max 8")
    cause_area: SourcedCauseArea | None
    geography_served: Sourced[Geography] | None
    tax_status: Sourced[str] | None = Field(
        description="e.g. '501(c)(3)', 'Registered charity in England and Wales'")
    annual_revenue: Evidenced[Revenue] | None = Field(
        description="Total revenue/income for one fiscal year; not a single gift, goal or budget")
    staff_count: Sourced[int] | None
    impact_metrics: list[Sourced[str]] = Field(description="Max 5 short quantified claims")
    leadership: list[LLMPerson] = Field(description="Named staff leaders and board members, max 15")
    general_contact: GeneralContact | None
    open_rfps: list[RFP]
    open_roles: list[LLMOpenRole] = Field(description="Current paid job openings only")
    recent_news: list[NewsItem] = Field(description="Up to 8 newest dated items")
    funders_and_partners: list[FunderPartner] = Field(description="Max 15")
    looks_like_nonprofit: bool
    not_nonprofit_reason: str | None


class GapFillExtraction(BaseModel):
    """Second-pass target: only the key fields the first pass left empty."""
    leadership: list[LLMPerson]
    general_contact: GeneralContact | None
    annual_revenue: Evidenced[Revenue] | None


class LinkPick(BaseModel):
    urls: list[str] = Field(description="At most 3 URLs, copied exactly from the candidate list")
    reason: str


# ---------------------------------------------------------------- final profile

class Person(LLMPerson):
    buyer_role: BuyerRole = "other"  # keyword rules on title, not the LLM


class OpenRole(LLMOpenRole):
    is_buying_signal: bool = False  # keyword rules on title, not the LLM


class Tool(BaseModel):
    tool: str
    category: str
    source_url: str
    matched: str  # the URL/snippet that matched the fingerprint, for verification


class FinancialYear(BaseModel):
    fiscal_year: int
    revenue: float | None
    expenses: float | None
    source_url: str


class Identity(BaseModel):
    last_checked: datetime | None = None
    name: Sourced[str] | None = None
    legal_name: Sourced[str] | None = None
    website: str | None = None
    registration_id: Evidenced[str] | None = None
    country: Sourced[str] | None = None
    hq_location: Sourced[str] | None = None
    mission: Sourced[str] | None = None
    programs: list[Program] = []
    cause_area: SourcedCauseArea | None = None
    geography_served: Sourced[Geography] | None = None
    impact_metrics: list[Sourced[str]] = []
    looks_like_nonprofit: bool | None = None


class Financials(BaseModel):
    last_checked: datetime | None = None
    annual_revenue: Evidenced[Revenue] | None = None
    staff_count: Sourced[int] | None = None
    size_band: SizeBand = "unknown"
    financial_history: list[FinancialYear] | None = None  # registry filings (US only for now)
    tax_status: Sourced[str] | None = None


class Contacts(BaseModel):
    last_checked: datetime | None = None
    leadership: list[Person] = []
    general_contact: GeneralContact | None = None


class Signals(BaseModel):
    last_checked: datetime | None = None
    open_rfps: list[RFP] = []
    open_roles: list[OpenRole] = []
    recent_news: list[NewsItem] = []
    funders_and_partners: list[FunderPartner] = []


class Technology(BaseModel):
    last_checked: datetime | None = None
    tech_stack: list[Tool] = []


class PageRecord(BaseModel):
    url: str
    page_type: str  # discover category: homepage, about, team, careers, ...
    kind: DocKind | None = None
    status: int | None = None  # HTTP status; None if the request never completed
    tokens: int = 0
    error: str | None = None
    via: str = "crawl"  # crawl | gapfill


class CallCost(BaseModel):
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cached_locally: bool = False
    usd: float | None  # what this call actually cost (0 on a local cache hit)
    usd_list: float | None  # what it would cost uncached


class RunCost(BaseModel):
    calls: list[CallCost] = []
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    usd_list: float = 0.0


class GapFill(BaseModel):
    triggered: bool = False
    missing: list[str] = []
    urls: list[str] = []
    filled: list[str] = []
    note: str | None = None


class NonprofitProfile(BaseModel):
    schema_version: str = SCHEMA_VERSION
    taxonomy_version: str = TAXONOMY_VERSION
    input: str
    resolved_url: str | None = None
    extracted_at: datetime
    status: Status
    flags: list[str] = []
    identity: Identity = Identity()
    financials: Financials = Financials()
    contacts: Contacts = Contacts()
    signals: Signals = Signals()
    technology: Technology = Technology()
    pages_crawled: list[PageRecord] = []
    errors: list[str] = []
    gapfill: GapFill = GapFill()
    cost: RunCost = RunCost()
