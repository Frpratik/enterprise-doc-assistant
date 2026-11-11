"""
Quality checks (the inspector). Runs AFTER parsing + cleaning.

Each check gives a number (saved in doc.quality.checks) and maybe a warning:
  text_chars          no usable text at all            -> FAILED (nothing to search later)
  chars_per_page      very little text per page        -> OCR probably missed things
  ocr_confidence      Tesseract's own confidence       -> blurry / faded scan
  ocr_low_pages       pages with low OCR confidence    -> tells you WHERE to look
  garbage_ratio       share of odd symbols             -> broken encoding / bad OCR
  page_coverage       pages that produced any text     -> blank or unreadable pages
  images_without_text images where OCR found nothing   -> maybe a chart, maybe a bad scan
  spreadsheet_tables  a spreadsheet with no tables     -> parsing went wrong

Score = the WEAKEST sub-score (0..1), like a chain that is only as strong as its weakest link.
An average would hide one bad part (a 0.69 OCR page averaged with four perfect checks = 0.94).
Any warning -> NEEDS_REVIEW, else DONE.
Note: OCR confidence is NOT accuracy (Tesseract can be sure and still wrong).
Real accuracy is measured with the golden set (Stage 8).
"""
import unicodedata
from collections import defaultdict

from ingestion.config import settings
from ingestion.models import Document, ElementType, Status

T = ElementType
REAL = {T.HEADING, T.PARAGRAPH, T.LIST_ITEM, T.TABLE, T.IMAGE_OCR, T.NOTES}   # placeholders don't count as text
OCR = {T.IMAGE_OCR}


def garbage_ratio(text: str) -> float:
    """Share of characters that are not letters, numbers, spaces, punctuation or currency."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for c in chars
              if unicodedata.category(c)[0] not in ("L", "N", "P") and unicodedata.category(c) not in ("Sc", "Sm"))
    return bad / len(chars)


def check_quality(doc: Document) -> Document:
    q = doc.quality
    real = [e for e in doc.elements if e.type in REAL and e.text.strip()]
    text = "\n".join(e.text for e in real)
    total = len(text)
    q.checks["text_chars"] = total

    # ---- 1. nothing usable -> failed ----
    if total == 0:
        q.score = 0.0
        q.warnings.append("no text could be extracted (OCR found nothing readable)")
        doc.mark(Status.FAILED, q.warnings[-1])
        return doc

    sub_scores = []
    unit_of = _unit_key(doc)            # page / slide / sheet, or None (Word has no pages)
    n_units = doc.metadata.page_count or 0

    # ---- 2. text per page ----
    if unit_of and n_units:
        cpp = total / n_units
        q.checks["chars_per_page"] = round(cpp, 1)
        sub_scores.append(min(1.0, cpp / settings.min_chars_per_page))
        if cpp < settings.min_chars_per_page:
            q.warnings.append(f"very little text: {cpp:.0f} characters per page "
                              f"(expected at least {settings.min_chars_per_page})")

    # ---- 3. OCR confidence (weighted by text length) + which pages are weak ----
    ocr = [e for e in real if e.ocr_confidence is not None]
    if ocr:
        weight = sum(len(e.text) for e in ocr)
        conf = sum(e.ocr_confidence * len(e.text) for e in ocr) / weight
        q.checks["ocr_confidence"] = round(conf, 3)
        sub_scores.append(min(e.ocr_confidence for e in ocr))     # the weakest OCR piece
        by_unit = defaultdict(list)
        for e in ocr:
            by_unit[e.location.label()].append(e.ocr_confidence)
        low = sorted(u for u, cs in by_unit.items() if min(cs) < settings.min_ocr_confidence)
        q.checks["ocr_low_pages"] = len(low)
        if conf < settings.min_ocr_confidence or low:
            q.warnings.append(f"low OCR confidence {conf:.2f} (minimum {settings.min_ocr_confidence})"
                              + (f" on: {', '.join(low)}" if low else ""))

    # ---- 4. garbage characters ----
    prose = "\n".join(e.text for e in real if e.type != T.TABLE)
    g = garbage_ratio(prose)
    q.checks["garbage_ratio"] = round(g, 3)
    sub_scores.append(max(0.0, 1 - g / settings.max_garbage_ratio))
    if g > settings.max_garbage_ratio:
        q.warnings.append(f"{g:.0%} of characters look like garbage (limit {settings.max_garbage_ratio:.0%})")

    # ---- 5. page coverage ----
    if unit_of and n_units:
        with_text = {getattr(e.location, unit_of) for e in real}
        coverage = len(with_text) / n_units
        q.checks["page_coverage"] = round(coverage, 3)
        sub_scores.append(coverage)
        if coverage < settings.min_page_coverage:
            empty = sorted(set(range(1, n_units + 1)) - with_text) if unit_of != "sheet" else []
            q.warnings.append(f"only {len(with_text)} of {n_units} {unit_of}s have text"
                              + (f" (empty: {', '.join(map(str, empty))})" if empty else ""))

    # ---- 6. images where OCR found nothing (information, not always a problem) ----
    placeholders = sum(1 for e in doc.elements if e.type == T.IMAGE_PLACEHOLDER)
    q.checks["images_without_text"] = placeholders

    # ---- 7. spreadsheet must give tables ----
    if doc.file_type in ("xlsx", "csv") and not any(e.type == T.TABLE for e in real):
        q.warnings.append("spreadsheet but no tables were extracted")
        sub_scores.append(0.0)

    q.score = round(min(sub_scores), 3) if sub_scores else 1.0
    if q.warnings:
        doc.mark(Status.NEEDS_REVIEW, "; ".join(q.warnings))
    else:
        doc.mark(Status.DONE)
    return doc


def _unit_key(doc: Document):
    """Which Location field counts as a 'page' for this file type."""
    if doc.file_type == "pptx":
        return "slide"
    if doc.file_type in ("xlsx", "csv"):
        return "sheet"
    if doc.file_type in ("pdf", "image"):
        return "page"
    return None          # docx, txt, md, html: no real pages
