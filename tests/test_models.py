"""Stage 1 tests: the standard format works and rejects bad data."""
import pytest
from pydantic import ValidationError

from ingestion.models import Document, Element, ElementType, Location, PageKind, Status


def make_doc():
    doc = Document(doc_id="doc_test", file_name="leave_policy.pdf", file_type="pdf")
    doc.add(Element(type=ElementType.HEADING, text="Sick Leave", level=2, location=Location(page=1)))
    doc.add(Element(type=ElementType.PARAGRAPH, text="Employees get 12 sick leaves.", location=Location(page=1),
                    page_kind=PageKind.DIGITAL))
    doc.add(Element(type=ElementType.TABLE, text="| Type | Days |\n|---|---|\n| Sick | 12 |", location=Location(page=1)))
    return doc


def test_ids_are_given_in_order():
    doc = make_doc()
    assert [e.element_id for e in doc.elements] == ["el_0001", "el_0002", "el_0003"]


def test_counts_and_full_text():
    doc = make_doc()
    assert doc.counts() == {"heading": 1, "paragraph": 1, "table": 1}
    assert "12 sick leaves" in doc.full_text()


def test_json_round_trip():
    doc = make_doc()
    doc.mark(Status.DONE)
    again = Document.model_validate_json(doc.model_dump_json())
    assert again == doc


def test_bad_element_type_is_rejected():
    with pytest.raises(ValidationError):
        Element(type="video", text="not allowed")


def test_location_labels():
    assert Location(page=3).label() == "Page 3"
    assert Location(slide=2).label() == "Slide 2"
    assert Location(sheet="Leaves").label() == "Sheet 'Leaves'"
