"""Turn fetched bytes into clean, truncated text documents for the LLM.

HTML -> trafilatura main text (BeautifulSoup fallback) + mailto/tel links
PDF  -> pymupdf text: first pages + the pages with the most financial/leadership keywords
CSV  -> pandas header + first rows
XML  -> flattened leaf text (RSS feeds, data exports). Sitemaps are handled in discover.py.
"""
from __future__ import annotations

import io
import re
from urllib.parse import urlparse
from xml.etree import ElementTree

import pymupdf
import pandas as pd
import trafilatura
from bs4 import BeautifulSoup
from pydantic import BaseModel

from src.fetch import FetchResult, fetch
from src.schema import DocKind, PageRecord

CHARS_PER_TOKEN = 4  # rough estimate; real counts come back from the API
PER_DOC_TOKENS = {"html": 4000, "pdf": 6000, "csv": 1500, "xml": 2000}
TOTAL_TOKENS = 30000
NEAR_EMPTY_CHARS = 200

PDF_MAX_PAGES = 10
PDF_SCAN_PAGES = 40
PDF_KEYWORDS = re.compile(
    r"total (revenue|income|support)|statement of (activities|financial)|revenue|expenses|"
    r"net assets|board of (directors|trustees)|executive director|chief executive|trustees",
    re.I,
)


class Document(BaseModel):
    url: str
    kind: DocKind
    page_type: str = "other"
    text: str
    raw_html: str | None = None  # kept for tech-stack fingerprinting
    truncated: bool = False

    @property
    def est_tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN


def est_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def detect_kind(url: str, content_type: str | None) -> DocKind | None:
    ct = (content_type or "").lower()
    path = urlparse(url).path.lower()
    if "pdf" in ct or path.endswith(".pdf"):
        return "pdf"
    if "csv" in ct or path.endswith(".csv"):
        return "csv"
    if "html" in ct or path.endswith((".html", ".htm")):
        return "html"
    if "xml" in ct or "rss" in ct or "atom" in ct or path.endswith((".xml", ".rss")):
        return "xml"
    if not ct or ct.startswith("text/"):
        return "html"
    return None


def _clean(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def contact_links(html: str) -> list[str]:
    """mailto:/tel: links. Footers hold these and main-text extraction often drops footers."""
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            val = "email: " + href[7:].split("?")[0].strip()
        elif low.startswith("tel:"):
            val = "phone: " + href[4:].strip()
        else:
            continue
        if val not in found:
            found.append(val)
    return found[:15]


def _soup_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "form", "iframe", "template"]):
        tag.decompose()
    return _clean(soup.get_text("\n"))


def footer_text(html: str, limit: int = 1200) -> str:
    """Tail of the site footer: where registration numbers, legal names and addresses live.
    Both trafilatura and the nav-stripping fallback tend to drop it."""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.find_all("footer") or soup.select('[id*="footer"], [class*="footer"]')[:2]
    text = _clean(" ".join(b.get_text(" ", strip=True) for b in blocks))
    return text[-limit:]


def html_to_text(html: str, url: str, include_footer: bool = False, prefer_full: bool = False) -> str:
    main = trafilatura.extract(html, url=url, favor_recall=True, include_tables=True,
                               include_comments=False, include_links=False) or ""
    full = _soup_text(html)
    # trafilatura sometimes drops people grids and card layouts (names in card headings);
    # on team pages, or when it kept less than 40% of the visible text, the fuller
    # (noisier) version is the safer input.
    text = full if prefer_full or len(main) < 0.4 * len(full) else main
    if include_footer and (footer := footer_text(html)) and footer[-200:] not in text:
        text += "\n\n[Site footer]\n" + footer
    links = contact_links(html)
    if links:
        text += "\n\n[Contact links in page HTML]\n" + "\n".join(links)
    return _clean(text)


def is_near_empty(text: str) -> bool:
    """Visible text too short to be a real page: usually a JS-rendered site."""
    body = text.split("[Contact links in page HTML]")[0]
    return len(body.strip()) < NEAR_EMPTY_CHARS


def pdf_to_text(content: bytes) -> str:
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        pages = [doc[i].get_text() for i in range(min(len(doc), PDF_SCAN_PAGES))]
    # Annual reports put mission up front and financials near the back: keep the first
    # two pages plus the pages that mention financial/leadership terms most.
    hits = [len(PDF_KEYWORDS.findall(p)) for p in pages]
    first = list(range(min(2, len(pages))))
    rest = sorted((i for i in range(len(pages)) if i not in first), key=lambda i: (-hits[i], i))
    keep = sorted(first + rest[: PDF_MAX_PAGES - len(first)])
    text = "\n".join(f"[page {i + 1}]\n{pages[i]}" for i in keep if pages[i].strip())
    if not text.strip():
        raise ValueError("PDF has no text layer (scanned image?)")
    return _clean(text)


def csv_to_text(content: bytes, max_rows: int = 20) -> str:
    df = pd.read_csv(io.BytesIO(content), nrows=max_rows, sep=None, engine="python",
                     encoding_errors="replace")
    return f"CSV columns: {', '.join(map(str, df.columns))}\n" + df.to_csv(index=False)


def xml_to_text(content: bytes, max_items: int = 300) -> str:
    root = ElementTree.fromstring(content)
    lines = [f"XML root: {_local(root.tag)}"]
    for el in root.iter():
        txt = (el.text or "").strip()
        if txt and len(el) == 0:
            lines.append(f"{_local(el.tag)}: {txt[:300]}")
            if len(lines) >= max_items:
                break
    return "\n".join(lines)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def truncate(text: str, max_tokens: int) -> tuple[str, bool]:
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text, False
    return text[:limit].rsplit(" ", 1)[0] + "\n[...truncated]", True


def parse(fr: FetchResult, page_type: str = "other") -> Document:
    """Raises ValueError on unsupported or unparseable content; the caller logs it."""
    url = fr.final_url or fr.url
    kind = detect_kind(url, fr.content_type)
    if kind is None:
        raise ValueError(f"unsupported content type {fr.content_type!r}")
    raw_html = None
    try:
        if kind == "html":
            raw_html = fr.text
            text = html_to_text(raw_html, url, include_footer=page_type == "homepage",
                                prefer_full=page_type == "team")
        elif kind == "pdf":
            text = pdf_to_text(fr.content)
        elif kind == "csv":
            text = csv_to_text(fr.content)
        else:
            text = xml_to_text(fr.content)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - library errors vary by format
        raise ValueError(f"could not parse {kind}: {type(e).__name__}: {e}") from e
    text, cut = truncate(text, PER_DOC_TOKENS[kind])
    return Document(url=url, kind=kind, page_type=page_type, text=text, raw_html=raw_html, truncated=cut)


def fit_to_budget(docs: list[Document], total_tokens: int = TOTAL_TOKENS) -> tuple[list[Document], list[str]]:
    """Keep docs in priority order until the token budget runs out; trim the last one."""
    kept, dropped, used = [], [], 0
    for d in docs:
        left = total_tokens - used
        if left < 300:
            dropped.append(d.url)
            continue
        if d.est_tokens > left:
            text, _ = truncate(d.text, left)
            d = d.model_copy(update={"text": text, "truncated": True})
        kept.append(d)
        used += d.est_tokens
    return kept, dropped


def load(url: str, page_type: str, via: str = "crawl") -> tuple[Document | None, PageRecord]:
    """Fetch + parse one URL. Never raises: failures end up in the PageRecord."""
    fr = fetch(url)
    rec = PageRecord(url=url, page_type=page_type, status=fr.status, error=fr.error, via=via)
    if not fr.ok:
        return None, rec
    try:
        doc = parse(fr, page_type)
    except ValueError as e:
        rec.error = str(e)
        return None, rec
    rec.kind, rec.tokens = doc.kind, doc.est_tokens
    return doc, rec
