"""
Clean = tidy the extracted text WITHOUT changing its meaning.

  1. fix odd characters    (ligatures like 'ﬁ', non-breaking spaces, invisible characters)
  2. join broken words     ('employ- ees' -> 'employees', from line-break hyphens)
  3. tidy spaces           (no double spaces, no extra blank lines)
  4. remove repeated headers/footers and page numbers (same line on most pages)
  5. drop elements that became empty
  6. detect the document language

Tables keep their Markdown layout (only odd characters are fixed inside them).
Counts of what was changed are saved in doc.quality.checks so we can see the effect.
"""
import re
import unicodedata
from collections import defaultdict

from langdetect import DetectorFactory, LangDetectException, detect

from ingestion.models import Document, ElementType

DetectorFactory.seed = 0      # langdetect is random by default; make results repeatable

INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)   # zero-width chars, soft hyphen
BROKEN_WORD = re.compile(r"\b([A-Za-z]{2,})- ([a-z]{2,})\b")                     # 'perfor- mance'
PAGE_NUMBER = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$|^\s*-\s*\d+\s*-\s*$", re.IGNORECASE)


def fix_characters(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)        # 'ﬁ' -> 'fi', full-width chars -> normal
    text = text.translate(INVISIBLE)
    text = text.replace(" ", " ").replace("\t", " ")
    return "".join(c for c in text if c == "\n" or unicodedata.category(c)[0] != "C")   # drop control chars


def join_broken_words(text: str) -> tuple[str, int]:
    return BROKEN_WORD.subn(r"\1\2", text)


def tidy_spaces(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _pattern(text: str) -> str:
    """'Page 3 of 10' and 'Page 4 of 10' -> same pattern 'page # of #'."""
    return re.sub(r"\d+", "#", text.lower()).strip()


def find_repeated_lines(doc: Document, min_share: float = 0.6, min_pages: int = 3) -> set[str]:
    """Short lines that appear on most pages = headers / footers."""
    pages_by_pattern = defaultdict(set)
    pages = {e.location.page for e in doc.elements if e.location.page}
    if len(pages) < min_pages:
        return set()
    for e in doc.elements:
        if e.location.page and e.type in (ElementType.PARAGRAPH, ElementType.HEADING) and len(e.text) <= 100:
            pages_by_pattern[_pattern(e.text)].add(e.location.page)
    return {p for p, seen in pages_by_pattern.items() if len(seen) / len(pages) >= min_share}


def clean_document(doc: Document) -> Document:
    joined = removed_repeats = removed_page_numbers = dropped_empty = 0
    repeated = find_repeated_lines(doc)

    kept = []
    for e in doc.elements:
        text = fix_characters(e.text)
        if e.type != ElementType.TABLE:
            text, n = join_broken_words(text)
            joined += n
            text = tidy_spaces(text)
            if _pattern(text) in repeated:
                removed_repeats += 1
                continue
            if e.location.page and PAGE_NUMBER.match(text):
                removed_page_numbers += 1
                continue
        if not text.strip():
            dropped_empty += 1
            continue
        e.text = text
        kept.append(e)

    # renumber ids so they stay el_0001, el_0002... after removals
    doc.elements = []
    for e in kept:
        doc.add(e)

    doc.metadata.language = detect_language(doc.full_text())
    doc.quality.checks.update({
        "clean_joined_words": joined,
        "clean_removed_headers_footers": removed_repeats,
        "clean_removed_page_numbers": removed_page_numbers,
        "clean_dropped_empty": dropped_empty,
    })
    return doc


def detect_language(text: str) -> str | None:
    """'en', 'hi', 'mr'... Needs enough text to be reliable."""
    sample = " ".join(text.split())[:5000]
    if len(sample) < 50:
        return None
    try:
        return detect(sample)
    except LangDetectException:
        return None
