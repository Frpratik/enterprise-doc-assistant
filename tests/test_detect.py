"""Stage 3 tests: per-page detection, labels, and the parser registry."""
import pytest

from ingestion import registry
from ingestion.detect import classify_pdf, final_label, initial_label
from ingestion.models import Document, Element, ElementType, PageKind
from ingestion.parsers.base import BaseParser

D, S, M = PageKind.DIGITAL, PageKind.SCANNED, PageKind.MIXED


@pytest.mark.parametrize("name,expected", [
    ("leave_policy.pdf", [D, D]),
    ("scanned_code_of_conduct.pdf", [S, S]),
    ("mixed_quarterly_report.pdf", [M, S]),     # page 1 text+image, page 2 only an image
])
def test_pdf_pages_are_classified(samples, name, expected):
    assert classify_pdf(samples / "good" / name) == expected


@pytest.mark.parametrize("file_type,kinds,label", [
    ("pdf", [D, D], "text-heavy"),
    ("pdf", [S, S], "scanned"),
    ("pdf", [M, S], "scanned"),
    ("pdf", [D, D, D, S], "text-heavy"),
    ("pptx", None, "slides"),
    ("xlsx", None, "spreadsheet"),
    ("csv", None, "spreadsheet"),
    ("image", None, "image"),
    ("docx", None, "text-heavy"),
])
def test_initial_label(file_type, kinds, label):
    assert initial_label(file_type, kinds) == label


def test_final_label_table_heavy():
    doc = Document(doc_id="d", file_name="x.docx", doc_label="text-heavy")
    doc.add(Element(type=ElementType.PARAGRAPH, text="Short intro."))
    doc.add(Element(type=ElementType.TABLE, text="| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 20))
    assert final_label(doc) == "table-heavy"


def test_registry_routes_and_rejects():
    @registry.register
    class FakeParser(BaseParser):
        name = "fake"
        file_types = ("fake_type",)
        def parse(self, doc, path):
            return []

    assert isinstance(registry.get_parser("fake_type"), FakeParser)
    with pytest.raises(registry.NoParserError):
        registry.get_parser("video")


def test_registry_blocks_two_parsers_for_one_type():
    @registry.register
    class A(BaseParser):
        file_types = ("dup_type",)
        def parse(self, doc, path): return []

    with pytest.raises(ValueError):
        @registry.register
        class B(BaseParser):
            file_types = ("dup_type",)
            def parse(self, doc, path): return []
