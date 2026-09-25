from src.discover import (
    Candidate, base_domain, discover, host_allowed, parse_sitemap, pdf_links, score_link, select,
)

HOME = "https://www.riverbendfoodbank.org/"


def test_link_scoring_order():
    about, _ = score_link("https://x.org/about-us/")
    privacy, cat = score_link("https://x.org/privacy-policy/")
    report, report_cat = score_link("https://x.org/wp-content/uploads/annual-report-2025.pdf", this_year=2026)
    assert about > 0 > privacy and cat == "skip"
    assert report_cat == "financials" and report >= about


def test_last_segment_decides_category():
    assert score_link("https://x.org/about/financials")[1] == "financials"
    assert score_link("https://x.org/about/executive-team")[1] == "team"


def test_word_boundaries_avoid_false_negatives():
    assert score_link("https://x.org/research")[0] >= 0  # not "search"
    assert score_link("https://x.org/accountability")[1] == "financials"  # not "account"
    assert score_link("https://x.org/newsletter-signup")[0] < 0


def test_deep_posts_rank_below_index_pages():
    assert score_link("https://x.org/news/")[0] > score_link("https://x.org/news/2024/05/some-post")[0]


def test_base_domain_and_allowlist():
    assert base_domain("www.oxfam.org.uk") == "oxfam.org.uk"
    assert base_domain("give.charitywater.org") == "charitywater.org"
    assert host_allowed("https://boards.greenhouse.io/x", "riverbendfoodbank.org")
    assert not host_allowed("https://www.facebook.com/x", "riverbendfoodbank.org")


def test_discover_on_fixture(fixture_bytes):
    sitemap, _ = parse_sitemap(fixture_bytes("sitemap.xml"))
    cands = discover(fixture_bytes("homepage.html").decode(), HOME, sitemap)
    urls = {c.url for c in cands}
    by_url = {c.url: c for c in cands}
    assert "https://www.riverbendfoodbank.org/privacy-policy/" not in urls
    assert "https://www.facebook.com/riverbendfoodbank" not in urls
    assert by_url["https://boards.greenhouse.io/riverbendfoodbank"].category == "careers"
    assert by_url["https://www.riverbendfoodbank.org/feed/"].category == "news_feed"
    assert "https://www.riverbendfoodbank.org/comments/feed/" not in urls
    assert by_url["https://www.riverbendfoodbank.org/about-us/board-of-directors/"].source == "sitemap"
    assert HOME not in urls


def test_sitemap_index_detected(fixture_bytes):
    locs, is_index = parse_sitemap(fixture_bytes("sitemap_index.xml"))
    assert is_index and len(locs) == 2
    assert parse_sitemap(b"<not xml") == ([], False)


def test_select_covers_categories_then_fills_by_score():
    cands = [
        Candidate(url="https://x.org/about", score=10, category="about"),
        Candidate(url="https://x.org/history", score=9.5, category="about"),
        Candidate(url="https://x.org/team", score=10, category="team"),
        Candidate(url="https://x.org/jobs", score=8, category="careers"),
        Candidate(url="https://x.org/donate", score=5, category="donate"),
        Candidate(url="https://x.org/misc", score=2, category="other"),
    ]
    selected, remainder = select(cands, max_pages=4)
    assert [c.url for c in selected] == ["https://x.org/about", "https://x.org/team",
                                         "https://x.org/jobs", "https://x.org/donate"]
    assert {c.url for c in remainder} == {"https://x.org/history", "https://x.org/misc"}


def test_pdf_hop_prefers_newest_report_by_filename_year():
    html = """
    <a href="/wp-content/uploads/2025/12/Financial-Statements-FY2023.pdf">Audited Financial Statement 2023</a>
    <a href="/wp-content/uploads/2025/12/Annual-Report-2025.pdf">2025 Annual Report</a>
    <a href="/wp-content/uploads/2025/09/Whistleblower-Policy.pdf">Whistleblower Policy</a>
    <a href="https://other-site.org/report-2025.pdf">Partner report</a>
    """
    pdfs = pdf_links(html, "https://www.x.org/financials/")
    assert [c.url for c in pdfs][:2] == ["https://www.x.org/wp-content/uploads/2025/12/Annual-Report-2025.pdf",
                                         "https://www.x.org/wp-content/uploads/2025/12/Financial-Statements-FY2023.pdf"]
    assert all("Whistleblower" not in c.url and "other-site" not in c.url for c in pdfs)


def test_article_slugs_penalised():
    article, _ = score_link("https://x.org/news/inspiring-young-people-to-take-action-for-nature")
    index, _ = score_link("https://x.org/about-us/our-people")
    assert index - article >= 3


def test_pdf_hop_skips_stale_reports():
    html = '<a href="/files/annual-report-FY2017-web.pdf">Annual Report 2017</a>'
    assert pdf_links(html, "https://www.x.org/about/annual-reports/", this_year=2026) == []
    assert len(pdf_links(html, "https://www.x.org/about/annual-reports/", this_year=2019)) == 1
