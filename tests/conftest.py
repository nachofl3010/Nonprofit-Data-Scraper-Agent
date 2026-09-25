from pathlib import Path

import pymupdf
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_bytes():
    return lambda name: (FIXTURES / name).read_bytes()


@pytest.fixture
def annual_report_pdf() -> bytes:
    """A 14-page 'annual report': intro up front, financials on page 12. Built in code so no
    binary file is committed."""
    doc = pymupdf.open()
    for i in range(14):
        page = doc.new_page()
        if i == 0:
            text = "Riverbend Food Bank Annual Report 2025. Our mission is to end hunger."
        elif i == 11:
            text = "Statement of Activities. Total revenue $4,600,000. Total expenses $4,400,000."
        else:
            text = f"Stories from our partner pantries, page {i + 1}."
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data
