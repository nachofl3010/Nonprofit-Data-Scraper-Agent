from src.fetch import FetchResult
from src.parse import (
    Document, csv_to_text, detect_kind, fit_to_budget, html_to_text, is_near_empty, parse,
    pdf_to_text, truncate, xml_to_text,
)


def test_html_keeps_main_text_and_footer_contacts(fixture_bytes):
    text = html_to_text(fixture_bytes("homepage.html").decode(), "https://www.riverbendfoodbank.org/")
    assert "4.2 million meals" in text
    assert "email: info@riverbendfoodbank.org" in text
    assert "phone: +15551234567" in text


def test_js_shell_detected_as_near_empty(fixture_bytes):
    text = html_to_text(fixture_bytes("js_shell.html").decode(), "https://app.example.org/")
    assert is_near_empty(text)
    real = html_to_text(fixture_bytes("homepage.html").decode(), "https://www.riverbendfoodbank.org/")
    assert not is_near_empty(real)


def test_pdf_keeps_first_pages_and_financial_page(annual_report_pdf):
    text = pdf_to_text(annual_report_pdf)
    assert "[page 1]" in text and "Our mission" in text
    assert "[page 12]" in text and "Total revenue $4,600,000" in text
    assert text.count("[page ") <= 10


def test_csv_header_and_rows(fixture_bytes):
    text = csv_to_text(fixture_bytes("financials.csv"))
    assert text.startswith("CSV columns: fiscal_year, total_revenue, total_expenses")
    assert "2025,4600000,4400000" in text


def test_xml_rss_flattened(fixture_bytes):
    text = xml_to_text(fixture_bytes("feed.xml"))
    assert text.startswith("XML root: rss")
    assert "title: Riverbend opens new mobile market in Eastside" in text
    assert "pubDate: Tue, 02 Sep 2025" in text


def test_detect_kind():
    assert detect_kind("https://x.org/report.pdf", None) == "pdf"
    assert detect_kind("https://x.org/data", "text/csv") == "csv"
    assert detect_kind("https://x.org/feed/", "application/rss+xml") == "xml"
    assert detect_kind("https://x.org/about", "text/html") == "html"
    assert detect_kind("https://x.org/logo", "image/png") is None


def test_parse_dispatch_and_unsupported(annual_report_pdf):
    doc = parse(FetchResult(url="https://x.org/ar.pdf", status=200, content_type="application/pdf",
                            content=annual_report_pdf), "financials")
    assert doc.kind == "pdf" and doc.page_type == "financials" and doc.raw_html is None
    try:
        parse(FetchResult(url="https://x.org/img", status=200, content_type="image/png", content=b"x"))
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "unsupported" in str(e)


def test_truncate_and_budget():
    text, cut = truncate("word " * 1000, 100)
    assert cut and len(text) < 450 and text.endswith("[...truncated]")
    docs = [Document(url=f"https://x.org/{i}", kind="html", text="a" * 4000) for i in range(5)]
    kept, dropped = fit_to_budget(docs, total_tokens=2500)  # 1000 tokens each
    assert len(kept) == 3 and kept[-1].truncated
    assert dropped == ["https://x.org/3", "https://x.org/4"]


def test_homepage_footer_kept_even_inside_nav():
    html = ("<html><body><main><h1>Save the Birds</h1><p>" + "We protect wild birds and habitats. " * 30 +
            "</p></main><footer><nav><a href='/a'>About</a><a href='/b'>Jobs</a><p>The Society is a registered "
            "charity: England and Wales no. 207076</p></nav></footer></body></html>")
    assert "207076" not in html_to_text(html, "https://birds.org.uk/")
    assert "[Site footer]" in html_to_text(html, "https://birds.org.uk/", include_footer=True)
    assert "207076" in html_to_text(html, "https://birds.org.uk/", include_footer=True)


def test_team_pages_keep_name_headings():
    cards = "".join(f'<div class="card"><span><h2>{n}</h2></span><p>{bio}</p></div>' for n, bio in [
        ("Scott Harrison", "Scott spent almost 10 years as a nightclub promoter before founding the organisation. " * 3),
        ("Melissa Russell", "Melissa Russell is the President and leads global teams around the world. " * 3),
    ])
    html = f"<html><body><main><h1>Executive team</h1>{cards}</main></body></html>"
    text = parse(FetchResult(url="https://x.org/about/team", status=200, content_type="text/html",
                             content=html.encode()), "team").text
    assert "Scott Harrison" in text
