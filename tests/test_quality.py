"""Stage 6 tests: the inspector flags the right files with the right reasons."""
import pytest

from ingestion.clean import clean_document
from ingestion.models import Document, Element, ElementType, Location, Status
from ingestion.quality import check_quality, garbage_ratio
from ingestion.registry import get_parser
from ingestion.safety import FILE_TYPES


def run(samples, name):
    path = samples / "good" / name
    ft = FILE_TYPES[path.suffix.lower()]
    doc = Document(doc_id="t", file_name=name, file_type=ft)
    doc.elements = get_parser(ft).parse(doc, path)
    return check_quality(clean_document(doc))


@pytest.mark.parametrize("name", [
    "leave_policy.pdf", "scanned_code_of_conduct.pdf", "long_handbook.pdf", "employee_handbook.docx",
    "onboarding_deck.pptx", "salary_structure.xlsx", "holidays_2026.csv", "notice_board_photo.png",
    "it_policy.txt", "remote_work.md", "hr_faq.html",
])
def test_clean_files_are_done(samples, name):
    doc = run(samples, name)
    assert doc.status == Status.DONE, doc.quality.warnings
    assert doc.quality.score >= 0.9


def test_low_confidence_scan_needs_review(samples):
    doc = run(samples, "canteen_notice_scan.png")
    assert doc.status == Status.NEEDS_REVIEW
    assert "low OCR confidence" in doc.status_reason and "Page 1" in doc.status_reason
    assert doc.quality.score < 0.7


def test_blank_page_needs_review(samples):
    doc = run(samples, "policy_with_blank_page.pdf")
    assert doc.status == Status.NEEDS_REVIEW
    assert "only 2 of 3 pages" in doc.status_reason and "empty: 2" in doc.status_reason


def test_unreadable_scan_fails(samples):
    doc = run(samples, "faded_receipt_scan.png")
    assert doc.status == Status.FAILED
    assert "no text could be extracted" in doc.status_reason


def test_garbage_text_needs_review():
    doc = Document(doc_id="t", file_name="x.txt", file_type="txt")
    doc.add(Element(type=ElementType.PARAGRAPH, text="Leave ■■□□◆◆ ▓▓▒▒░░ ¤¤¤ ☐☐☐ policy ◆◆◆◆", location=Location()))
    check_quality(doc)
    assert doc.status == Status.NEEDS_REVIEW and "garbage" in doc.status_reason


def test_empty_spreadsheet_needs_review():
    doc = Document(doc_id="t", file_name="x.xlsx", file_type="xlsx")
    doc.metadata.page_count = 1
    doc.add(Element(type=ElementType.HEADING, text="Sheet1 has only a title and no table rows",
                    location=Location(sheet="Sheet1")))
    check_quality(doc)
    assert "no tables" in doc.status_reason


def test_garbage_ratio():
    assert garbage_ratio("Employees get 12 leaves. Cost: Rs 500 (₹500) = 100%") == 0
    assert garbage_ratio("■■■■ab") == pytest.approx(4 / 6)
