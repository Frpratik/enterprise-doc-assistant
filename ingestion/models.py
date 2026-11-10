"""
The ONE standard format of the pipeline.

Every parser (PDF, Word, PPT, Excel, image, text, HTML) must return Elements,
and every file ends up as one Document. Later stages (cleaning, quality checks,
Phase 2 chunking) only ever see this shape, never the original file type.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------- Small fixed lists (enums) ----------
class Status(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    NEEDS_REVIEW = "needs_review"          # extracted, but quality checks raised warnings
    FAILED = "failed"                      # could not be processed (reason is saved)
    SKIPPED_DUPLICATE = "skipped_duplicate"  # exact same file was already processed


class ElementType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"                        # text holds the table as Markdown
    IMAGE_OCR = "image_ocr"                # text read from an image by OCR
    IMAGE_PLACEHOLDER = "image_placeholder"  # image found, but no text in it
    NOTES = "notes"                        # e.g. PowerPoint speaker notes


class PageKind(str, Enum):
    DIGITAL = "digital"   # page has a real text layer
    SCANNED = "scanned"   # page is only an image -> OCR
    MIXED = "mixed"       # text layer + images with text


# ---------- Building blocks ----------
class Location(BaseModel):
    """Where an element came from (needed later for citations)."""
    page: Optional[int] = None    # PDF / DOCX (approx) / image = 1
    slide: Optional[int] = None   # PPTX
    sheet: Optional[str] = None   # XLSX / CSV

    def label(self) -> str:
        if self.slide is not None:
            return f"Slide {self.slide}"
        if self.sheet is not None:
            return f"Sheet '{self.sheet}'"
        if self.page is not None:
            return f"Page {self.page}"
        return "Document"


class Element(BaseModel):
    """One piece of content: a heading, a paragraph, a table, OCR text..."""
    element_id: str = ""                    # filled by the pipeline: el_0001, el_0002...
    type: ElementType
    text: str
    location: Location = Field(default_factory=Location)
    level: Optional[int] = None             # heading level (1 = biggest)
    ocr_confidence: Optional[float] = None  # 0..1, only for OCR elements
    page_kind: Optional[PageKind] = None    # only for PDF pages


class Metadata(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    created: Optional[str] = None
    page_count: Optional[int] = None        # pages / slides / sheets
    language: Optional[str] = None


class Quality(BaseModel):
    score: Optional[float] = None           # 0..1, filled by the quality stage
    warnings: list[str] = Field(default_factory=list)
    checks: dict[str, float] = Field(default_factory=dict)  # raw numbers behind the score


# ---------- The Document (one per file) ----------
class Document(BaseModel):
    doc_id: str
    file_name: str
    file_type: str = ""                     # pdf, docx, pptx, xlsx, csv, image, txt, md, html
    mime_type: Optional[str] = None
    size_bytes: int = 0
    hash: str = ""                          # SHA-256 of the file bytes
    version: int = 1                        # +1 when the same file name comes with new content
    doc_label: Optional[str] = None         # text-heavy / table-heavy / scanned / slides / spreadsheet
    source_path: Optional[str] = None       # where the file came from (inbox or user path)
    original_path: Optional[str] = None     # our untouched copy (source of truth)

    metadata: Metadata = Field(default_factory=Metadata)
    parser: Optional[str] = None
    parser_version: Optional[str] = None

    status: Status = Status.QUEUED
    status_reason: Optional[str] = None     # WHY it failed / needs review
    quality: Quality = Field(default_factory=Quality)
    elements: list[Element] = Field(default_factory=list)

    processed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None

    # ----- helpers -----
    def add(self, element: Element) -> None:
        """Add an element and give it the next id."""
        element.element_id = f"el_{len(self.elements) + 1:04d}"
        self.elements.append(element)

    def full_text(self) -> str:
        return "\n\n".join(e.text for e in self.elements if e.text)

    def counts(self) -> dict[str, int]:
        """How many elements of each type, e.g. {'heading': 4, 'table': 1}."""
        out: dict[str, int] = {}
        for e in self.elements:
            out[e.type.value] = out.get(e.type.value, 0) + 1
        return out

    def mark(self, status: Status, reason: Optional[str] = None) -> None:
        self.status = status
        self.status_reason = reason
        self.processed_at = datetime.now(timezone.utc)
