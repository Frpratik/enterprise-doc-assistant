"""Phase 2 Stage 1 tests: chunking by meaning, with context, within limits, nothing lost."""
import pytest

from indexing.chunk_checks import check_chunks
from indexing.chunker import chunk_document
from indexing.tokens import count_tokens
from ingestion.clean import clean_document
from ingestion.config import settings
from ingestion.models import Document, Element, ElementType, Location
from ingestion.registry import get_parser
from ingestion.safety import FILE_TYPES

T = ElementType
GOOD = ["leave_policy.pdf", "scanned_code_of_conduct.pdf", "mixed_quarterly_report.pdf", "long_handbook.pdf",
        "employee_handbook.docx", "onboarding_deck.pptx", "salary_structure.xlsx", "holidays_2026.csv",
        "notice_board_photo.png", "it_policy.txt", "remote_work.md", "hr_faq.html",
        "canteen_notice_scan.png", "policy_with_blank_page.pdf"]


_parsed: dict[str, str] = {}


def load(samples, name):
    """Parse + clean once per test run (OCR is slow), then hand out fresh copies."""
    if name not in _parsed:
        _parsed[name] = _parse(samples, name).model_dump_json()
    return Document.model_validate_json(_parsed[name])


def _parse(samples, name):
    path = samples / "good" / name
    ft = FILE_TYPES[path.suffix.lower()]
    doc = Document(doc_id="t", file_name=name, file_type=ft)
    for el in get_parser(ft).parse(doc, path):
        doc.add(el)
    return clean_document(doc)


@pytest.mark.parametrize("name", GOOD)
def test_every_sample_passes_chunk_checks(samples, name):
    doc = load(samples, name)
    chunks = chunk_document(doc)
    assert chunks, "no chunks"
    assert check_chunks(doc, chunks) == []


def test_sections_become_chunks_with_path_and_page(samples):
    chunks = chunk_document(load(samples, "leave_policy.pdf"))
    sick = next(c for c in chunks if "12 sick leaves" in c.content)
    assert sick.heading_path == ["Leave Policy 2026", "1. Sick Leave"]
    assert sick.location == {"pages": [1]}
    assert sick.embed_text.startswith("Leave Policy 2026 > 1. Sick Leave")
    notice = next(c for c in chunks if "60 days" in c.content)
    assert notice.location == {"pages": [2]} and "12 sick" not in notice.content    # sections stay separate


def test_table_is_its_own_chunk_with_heading(samples):
    chunks = chunk_document(load(samples, "leave_policy.pdf"))
    table = next(c for c in chunks if c.chunk_type == "table")
    assert table.content.startswith("Leave Summary\n| Leave Type |")
    assert "| Earned Leave | 18 | Yes, up to 30 days |" in table.content


def test_one_chunk_per_slide_with_notes(samples):
    chunks = chunk_document(load(samples, "onboarding_deck.pptx"))
    assert [c.location["slides"] for c in chunks] == [[1], [2], [3], [4]]
    assert "Speaker notes:" in chunks[0].content
    assert chunks[2].heading_path[-1] == "Key Contacts" and "it@acme-example.com" in chunks[2].content


def test_sheet_chunks_keep_header(samples):
    chunks = chunk_document(load(samples, "salary_structure.xlsx"))
    assert [c.location["sheet"] for c in chunks] == ["Salary Bands", "Components"]
    assert all(c.content.startswith("| ") for c in chunks)


def test_no_duplicate_title_in_path(samples):
    chunks = chunk_document(load(samples, "mixed_quarterly_report.pdf"))
    assert chunks[0].heading_path == ["Quarterly HR Report Q3 2026", "Headcount"]


def test_long_section_is_split_with_overlap():
    doc = Document(doc_id="t", file_name="big.pdf", file_type="pdf")
    doc.add(Element(type=T.HEADING, text="Travel Policy", level=1, location=Location(page=1)))
    sentences = [f"Rule {i}: employees travelling to city number {i} must book {i % 5 + 1} days ahead." for i in range(120)]
    doc.add(Element(type=T.PARAGRAPH, text=" ".join(sentences), location=Location(page=1)))
    chunks = chunk_document(doc)
    assert len(chunks) > 3
    assert all(c.token_count <= settings.chunk_max_tokens for c in chunks)
    assert [c.split_part for c in chunks] == list(range(1, len(chunks) + 1))
    last_sentence_of_first = chunks[0].content.split(". ")[-1]
    assert last_sentence_of_first.rstrip(".") in chunks[1].content                 # overlap carried over
    assert check_chunks(doc, chunks) == []


def test_big_table_split_by_rows_header_repeated():
    rows = ["| Emp ID | Name | Department |", "|---|---|---|"] + \
           [f"| E{i:04d} | Employee {i} | Department {i % 7} |" for i in range(300)]
    doc = Document(doc_id="t", file_name="staff.xlsx", file_type="xlsx")
    doc.add(Element(type=T.TABLE, text="\n".join(rows), location=Location(sheet="Staff")))
    chunks = chunk_document(doc)
    assert len(chunks) > 1
    assert all(c.content.startswith("| Emp ID | Name | Department |\n|---|---|---|") for c in chunks)
    body = [l for c in chunks for l in c.content.splitlines()[2:]]
    assert body == rows[2:]                                       # every row exactly once, in order
    assert check_chunks(doc, chunks) == []


def test_lost_element_is_detected():
    doc = Document(doc_id="t", file_name="x.pdf", file_type="pdf")
    doc.add(Element(type=T.PARAGRAPH, text="Something important.", location=Location(page=1)))
    chunks = chunk_document(doc)
    chunks[0].element_ids = []                                    # simulate a chunker bug
    assert any("is in no chunk" in p for p in check_chunks(doc, chunks))


def test_token_counter_is_close_for_plain_text():
    assert 5 <= count_tokens("Employees get 12 sick leaves per year.") <= 15
