"""
PDF parser. Every page is handled by its kind (decided in detect.py):

  digital -> text blocks (PyMuPDF) + tables (pdfplumber)
  scanned -> render the page as an image -> OCR
  mixed   -> digital steps + OCR of the images on the page

Headings are found by FONT SIZE: text clearly bigger than normal body text
(or short + bold) is a heading; bigger = higher level.
Text inside a table is NOT repeated as a paragraph.
Pages are processed one at a time (big PDFs stay light on memory).
"""
from collections import Counter
from pathlib import Path

import pdfplumber
import pymupdf
from PIL import Image

from ingestion.config import settings
from ingestion.detect import classify_page
from ingestion.models import Document, Element, ElementType, Location, PageKind
from ingestion.parsers.base import BaseParser
from ingestion.parsers.image import image_elements
from ingestion.registry import register
from ingestion.textutils import rows_to_markdown

BOLD = 16   # PyMuPDF span flag for bold


def _pdf_date(raw: str | None) -> str | None:
    """PDF dates look like 'D:20261110160445+05'00''. Turn them into '2026-11-10T16:04:45'."""
    if not raw:
        return None
    d = raw[2:] if raw.startswith("D:") else raw
    if len(d) >= 14 and d[:14].isdigit():
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}T{d[8:10]}:{d[10:12]}:{d[12:14]}"
    return raw


def _inside(bbox, boxes, margin=2) -> bool:
    """Is the centre of bbox inside any of the boxes?"""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return any(b[0] - margin <= cx <= b[2] + margin and b[1] - margin <= cy <= b[3] + margin for b in boxes)


@register
class PdfParser(BaseParser):
    name, file_types = "pdf_parser", ("pdf",)

    def parse(self, doc: Document, path: Path) -> list[Element]:
        out = []
        with pymupdf.open(path) as pdf, pdfplumber.open(path) as plumber:
            meta = pdf.metadata or {}
            doc.metadata.title = meta.get("title") or None
            author = (meta.get("author") or "").strip()
            doc.metadata.author = author if author and author.lower() not in ("(anonymous)", "unknown") else None
            doc.metadata.created = _pdf_date(meta.get("creationDate"))
            doc.metadata.page_count = pdf.page_count

            body_size, heading_levels = self._font_profile(pdf)

            for i, page in enumerate(pdf):
                n = i + 1
                kind = classify_page(page)
                loc = Location(page=n)
                if kind == PageKind.SCANNED:
                    page_els = self._scanned_page(page, loc, doc)
                else:
                    page_els = self._digital_page(page, plumber.pages[i], loc, body_size, heading_levels)
                    if kind == PageKind.MIXED:
                        page_els += self._page_images(pdf, page, loc, doc)
                for _, el in sorted(page_els, key=lambda t: (round(t[0][1]), t[0][0])):   # top->bottom, left->right
                    el.page_kind = kind
                    out.append(el)
        return out

    # ---------- font profile (for heading detection) ----------
    @staticmethod
    def _font_profile(pdf) -> tuple[float, dict[float, int]]:
        """Body size = the most common font size. Bigger sizes -> heading levels 1, 2, 3."""
        sizes = Counter()
        for page in pdf:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        sizes[round(span["size"], 1)] += len(span["text"].strip())
        if not sizes:
            return 0.0, {}
        body = sizes.most_common(1)[0][0]
        bigger = sorted((s for s in sizes if s >= body * 1.15), reverse=True)
        return body, {s: min(lvl, 6) for lvl, s in enumerate(bigger, start=1)}

    # ---------- digital page ----------
    def _digital_page(self, page, plumber_page, loc, body_size, heading_levels):
        items = []

        # 1. tables first, so we know which text belongs to them
        table_boxes = []
        for t in plumber_page.find_tables():
            rows = [r for r in t.extract() if r and any(c for c in r)]
            if len(rows) >= 2:
                table_boxes.append(t.bbox)
                items.append((t.bbox, Element(type=ElementType.TABLE, text=rows_to_markdown(rows), location=loc)))

        # 2. text blocks outside the tables
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0 or _inside(block["bbox"], table_boxes):
                continue
            spans = [s for line in block["lines"] for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = " ".join(" ".join(s["text"].strip() for s in line["spans"] if s["text"].strip())
                            for line in block["lines"]).strip()
            text = " ".join(text.split())
            if not text:
                continue
            size = round(max(s["size"] for s in spans), 1)
            all_bold = all(s["flags"] & BOLD for s in spans)
            if size in heading_levels and len(text) <= 150:
                el = Element(type=ElementType.HEADING, text=text, level=heading_levels[size], location=loc)
            elif all_bold and len(text) <= 80 and size >= body_size:
                el = Element(type=ElementType.HEADING, text=text, level=len(heading_levels) + 1, location=loc)
            else:
                el = Element(type=ElementType.PARAGRAPH, text=text, location=loc)
            items.append((block["bbox"], el))
        return items

    # ---------- images on a mixed page ----------
    def _page_images(self, pdf, page, loc, doc):
        items = []
        for img in page.get_images(full=True):
            xref = img[0]
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            pix = pymupdf.Pixmap(pdf, xref)
            if pix.n - pix.alpha >= 4:                       # CMYK -> RGB
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            pil = Image.frombytes("RGB" if pix.n - pix.alpha == 3 else "L", (pix.width, pix.height),
                                  pix.samples if not pix.alpha else pymupdf.Pixmap(pix, 0).samples)
            for el in image_elements(pil, loc, doc):
                items.append((tuple(rects[0]), el))
        return items

    # ---------- scanned page ----------
    def _scanned_page(self, page, loc, doc):
        pix = page.get_pixmap(dpi=settings.ocr_dpi)
        pil = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        # keep OCR order: give each element a growing y so sorting doesn't shuffle them
        return [((0, i, 0, i), el) for i, el in enumerate(image_elements(pil, loc, doc, what="scanned page"))]
