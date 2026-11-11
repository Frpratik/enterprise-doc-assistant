"""
Detect = look at the file BEFORE parsing and decide how to read it.

1. PDFs are classified PAGE BY PAGE (one PDF can mix all three):
     digital -> has a real text layer, no meaningful images   -> read text directly
     scanned -> no text layer, page is an image               -> OCR the page
     mixed   -> text layer AND images (images may hold text)  -> read text + OCR images
2. Every document gets a simple label (by rules, no AI):
     scanned / slides / spreadsheet / image / text-heavy  (table-heavy is decided after parsing)
"""
from pathlib import Path

from ingestion.config import settings
from ingestion.models import Document, ElementType, PageKind


def classify_page(page) -> PageKind:
    """page = a pymupdf Page."""
    text_chars = len(page.get_text().strip())
    page_area = page.rect.width * page.rect.height or 1
    image_area = sum((b["bbox"][2] - b["bbox"][0]) * (b["bbox"][3] - b["bbox"][1])
                     for b in page.get_image_info())
    has_text = text_chars >= settings.pdf_min_text_chars
    has_images = image_area / page_area >= settings.pdf_min_image_coverage

    if has_text and has_images:
        return PageKind.MIXED
    if not has_text and has_images:
        return PageKind.SCANNED
    return PageKind.DIGITAL          # text only (or a blank page)


def classify_pdf(path: Path) -> list[PageKind]:
    """One PageKind per page. Opens pages one at a time (big PDFs stay light on memory)."""
    import pymupdf
    with pymupdf.open(path) as doc:
        return [classify_page(page) for page in doc]


def initial_label(file_type: str, page_kinds: list[PageKind] | None = None) -> str:
    """Label before parsing, from the file type and page kinds."""
    if file_type == "pptx":
        return "slides"
    if file_type in ("xlsx", "csv"):
        return "spreadsheet"
    if file_type == "image":
        return "image"
    if page_kinds:
        scanned = sum(k == PageKind.SCANNED for k in page_kinds)
        if scanned / len(page_kinds) >= 0.5:
            return "scanned"
    return "text-heavy"


def final_label(doc: Document) -> str:
    """Refine after parsing: a text document that is mostly tables becomes table-heavy."""
    label = doc.doc_label or "text-heavy"
    if label != "text-heavy" or not doc.elements:
        return label
    table_chars = sum(len(e.text) for e in doc.elements if e.type == ElementType.TABLE)
    all_chars = sum(len(e.text) for e in doc.elements) or 1
    return "table-heavy" if table_chars / all_chars >= 0.5 else "text-heavy"
