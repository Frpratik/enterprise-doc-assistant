"""Stage 5 tests: cleaning fixes the mess without changing meaning."""
from ingestion.clean import clean_document, detect_language, fix_characters, join_broken_words, tidy_spaces
from ingestion.models import Document, Element, ElementType, Location
from ingestion.registry import get_parser

T = ElementType


def test_fix_characters():
    assert fix_characters("ﬁle of of​ﬁce") == "file of office"


def test_join_broken_words():
    assert join_broken_words("New employ- ees and perfor- mance") == ("New employees and performance", 2)
    assert join_broken_words("Mumbai - Pune")[1] == 0            # a real dash is left alone
    assert join_broken_words("Q3- Report")[1] == 0               # next word capitalised -> not a broken word


def test_tidy_spaces():
    assert tidy_spaces("  a   b \n\n\n\n c ") == "a b\n\nc"


def test_header_footer_and_broken_words_on_real_pdf(samples):
    doc = Document(doc_id="t", file_name="long_handbook.pdf")
    doc.elements = get_parser("pdf").parse(doc, samples / "good" / "long_handbook.pdf")
    clean_document(doc)
    text = doc.full_text()
    assert "INTERNAL USE ONLY" not in text
    assert "Page 1 of 4" not in text
    assert "New employees" in text and "performance review" in text
    # 4 headers + 4 "Page N of 4" footers (same pattern on every page)
    assert doc.quality.checks["clean_removed_headers_footers"] == 8
    assert doc.quality.checks["clean_joined_words"] == 2
    assert [e.element_id for e in doc.elements][:2] == ["el_0001", "el_0002"]    # renumbered


def test_short_docs_keep_everything():
    """With < 3 pages we don't guess headers/footers (too risky)."""
    doc = Document(doc_id="t", file_name="x.pdf")
    for p in (1, 2):
        doc.add(Element(type=T.PARAGRAPH, text="Same line", location=Location(page=p)))
    clean_document(doc)
    assert len(doc.elements) == 2


def test_lone_page_number_removed():
    doc = Document(doc_id="t", file_name="x.pdf")
    doc.add(Element(type=T.PARAGRAPH, text="Real content here.", location=Location(page=1)))
    doc.add(Element(type=T.PARAGRAPH, text="- 7 -", location=Location(page=1)))
    clean_document(doc)
    assert [e.text for e in doc.elements] == ["Real content here."]
    assert doc.quality.checks["clean_removed_page_numbers"] == 1


def test_tables_keep_layout():
    doc = Document(doc_id="t", file_name="x.pdf")
    table = "| A  | B |\n|---|---|\n| 1 | 2 |"
    doc.add(Element(type=T.TABLE, text=table, location=Location(page=1)))
    clean_document(doc)
    assert doc.elements[0].text == table


def test_language():
    assert detect_language("Employees get twelve sick leaves per year and eight casual leaves.") == "en"
    assert detect_language("कर्मचारियों को हर साल बारह बीमारी की छुट्टियाँ और आठ आकस्मिक छुट्टियाँ मिलती हैं।") == "hi"
    assert detect_language("too short") is None
