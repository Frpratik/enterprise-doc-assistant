"""Stage 4 tests: every parser reads its sample file correctly."""
import pytest

from ingestion.models import Document, ElementType, PageKind
from ingestion.registry import get_parser, missing_types

T = ElementType


def parse(samples, name, file_type):
    doc = Document(doc_id="t", file_name=name)
    elements = get_parser(file_type).parse(doc, samples / "good" / name)
    return doc, elements


def texts(elements, type_=None):
    return [e.text for e in elements if type_ is None or e.type == type_]


def has(elements, needle, type_=None):
    return any(needle.lower() in t.lower() for t in texts(elements, type_))


def test_every_allowed_type_has_a_parser():
    assert missing_types() == set()


def test_txt(samples):
    _, els = parse(samples, "it_policy.txt", "txt")
    assert texts(els, T.HEADING) == ["IT POLICY"]
    assert has(els, "every 90 days", T.PARAGRAPH)
    assert not any("\r" in e.text for e in els)                 # Windows line endings cleaned


def test_markdown(samples):
    _, els = parse(samples, "remote_work.md", "md")
    assert [(e.text, e.level) for e in els if e.type == T.HEADING] == [
        ("Remote Work Policy", 1), ("Eligibility", 2), ("Equipment", 2)]
    assert has(els, "| Laptop | Yes |", T.TABLE)


def test_html_drops_junk(samples):
    doc, els = parse(samples, "hr_faq.html", "html")
    assert doc.metadata.title == "HR FAQ"
    assert has(els, "last working day", T.PARAGRAPH)
    assert has(els, "payroll@acme-example.com", T.TABLE)
    for junk in ["tracking", "Home | About", "Copyright"]:
        assert not has(els, junk)


def test_csv(samples):
    _, els = parse(samples, "holidays_2026.csv", "csv")
    table = texts(els, T.TABLE)[0]
    assert "| Date | Holiday |" in table and "Diwali" in table


def test_xlsx_every_sheet(samples):
    doc, els = parse(samples, "salary_structure.xlsx", "xlsx")
    assert doc.metadata.page_count == 2
    assert {e.location.sheet for e in els} == {"Salary Bands", "Components"}
    assert has(els, "| L3 | 15 | 25 |", T.TABLE)                # 15.0 shown as 15
    assert has(els, "HRA", T.TABLE)


def test_docx(samples):
    doc, els = parse(samples, "employee_handbook.docx", "docx")
    assert doc.metadata.title == "Employee Handbook"
    assert ("Benefits", 2) in [(e.text, e.level) for e in els if e.type == T.HEADING]
    assert len(texts(els, T.LIST_ITEM)) == 3
    assert has(els, "| Internet | 1,000 |", T.TABLE)
    assert has(els, "4455", T.IMAGE_OCR)                        # text inside the picture


def test_pptx(samples):
    doc, els = parse(samples, "onboarding_deck.pptx", "pptx")
    assert doc.metadata.page_count == 4
    assert [e.text for e in els if e.type == T.HEADING and e.location.slide == 1] == ["New Joiner Onboarding"]
    assert "Meet your buddy" in [e.text for e in els if e.type == T.LIST_ITEM and e.location.slide == 2]
    assert has(els, "it@acme-example.com", T.TABLE)
    assert any(e.location.slide == 4 and "Welcome to Acme" in e.text for e in els if e.type == T.IMAGE_OCR)
    assert has(els, "greet everyone", T.NOTES)


def test_image_ocr(samples):
    _, els = parse(samples, "notice_board_photo.png", "image")
    assert has(els, "15 November 2026") and has(els, "parking area")
    assert all(e.ocr_confidence and e.ocr_confidence > 0.6 for e in els)


def test_pdf_digital(samples):
    doc, els = parse(samples, "leave_policy.pdf", "pdf")
    assert doc.metadata.created.startswith("2026-")
    assert ("1. Sick Leave", 2) in [(e.text, e.level) for e in els if e.type == T.HEADING]
    assert has(els, "| Earned Leave | 18 | Yes, up to 30 days |", T.TABLE)
    assert not has(els, "Days per Year", T.PARAGRAPH)           # table text not repeated
    assert any(e.location.page == 2 and "60 days" in e.text for e in els)
    assert all(e.page_kind == PageKind.DIGITAL for e in els)


def test_pdf_scanned(samples):
    _, els = parse(samples, "scanned_code_of_conduct.pdf", "pdf")
    assert all(e.page_kind == PageKind.SCANNED for e in els)
    assert any(e.location.page == 1 and "Office hours" in e.text for e in els)
    assert any(e.location.page == 2 and "Friday" in e.text for e in els)


def test_pdf_mixed(samples):
    doc, els = parse(samples, "mixed_quarterly_report.pdf", "pdf")
    assert doc.metadata.author is None                          # '(anonymous)' dropped
    p1 = [e for e in els if e.location.page == 1]
    assert has(p1, "| Engineering | 210 | 6.5 |", T.TABLE)
    assert has(p1, "Satisfaction Score: 4.2", T.IMAGE_OCR)      # OCR of the picture on a text page
    assert all(e.page_kind == PageKind.MIXED for e in p1)
    assert any(e.location.page == 2 and "Finance Head" in e.text for e in els)
