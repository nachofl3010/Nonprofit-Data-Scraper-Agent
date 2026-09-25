from src.parse import Document
from src.techstack import detect, detect_html

HTML = """
<html><head>
<script src="https://bbox.blackbaudhosting.com/webforms/bbox-min.js"></script>
<link rel="stylesheet" href="/wp-content/themes/x/style.css">
</head><body>
<p>We are proud to be supported by Salesforce and Eventbrite employees.</p>
<iframe src="https://donorbox.org/embed/annual-appeal"></iframe>
<form action="https://org.us5.list-manage.com/subscribe/post"></form>
<a href="https://boards.greenhouse.io/org">Jobs</a>
<script>window.FundraiseUp = {}; var s = "https://cdn.fundraiseup.com/widget/ABC";</script>
</body></html>
"""


def test_detects_tools_from_attributes_and_inline_scripts():
    tools = {t.tool: t for t in detect_html(HTML, "https://org.org/donate")}
    assert "Blackbaud (Raiser's Edge NXT / Online Express)" in tools
    assert tools["Donorbox"].category == "donation_platform"
    assert "Mailchimp" in tools and "Greenhouse" in tools and "WordPress" in tools
    assert "Fundraise Up" in tools
    assert tools["Donorbox"].source_url == "https://org.org/donate"
    assert "donorbox.org" in tools["Donorbox"].matched


def test_visible_text_mentions_do_not_count():
    tools = {t.tool for t in detect_html(HTML, "https://org.org/")}
    assert "Salesforce" not in tools and "Eventbrite" not in tools


def test_detect_dedupes_across_pages_and_sorts_crm_first():
    docs = [Document(url=f"https://org.org/{i}", kind="html", text="", raw_html=HTML) for i in range(2)]
    tools = detect(docs)
    assert len({t.tool for t in tools}) == len(tools)
    assert tools[0].category == "donor_crm"
    assert all(t.source_url == "https://org.org/0" for t in tools)


def test_outbound_links_do_not_set_cms():
    html = '<a href="https://county.gov/sites/default/files/guide.pdf">County guide</a>' \
           '<link rel="stylesheet" href="https://static1.squarespace.com/static/x.css">'
    tools = {t.tool for t in detect_html(html, "https://org.org/")}
    assert tools == {"Squarespace"}
