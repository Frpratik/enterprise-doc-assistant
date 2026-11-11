"""
Chunker = cut a Document (Phase 1 output) into chunks by MEANING.

  slides (pptx)        -> one chunk per slide (title + bullets + tables + image text + speaker notes)
  sheets (xlsx / csv)  -> one chunk per table block, header always included
  everything else      -> one chunk per SECTION (a heading and the text under it);
                          tables get their own chunk inside their section

Every chunk keeps its context:
  heading_path  ['Leave Policy 2026', '1. Sick Leave']
  location      {'pages': [1]}  /  {'slides': [3]}  /  {'sheet': 'Salary Bands'}
  embed_text    'Leave Policy 2026 > 1. Sick Leave' + the content   (what the model embeds)

Size limits (tokens counted with the model's own tokenizer):
  a section bigger than chunk_max_tokens is split at sentence boundaries, with a small overlap;
  a big table is split by rows, header repeated in every piece.
"""
import re
from typing import Optional

from pydantic import BaseModel, Field

from indexing.tokens import count_tokens
from ingestion.config import settings
from ingestion.models import Document, Element, ElementType

T = ElementType
SKIP = {T.IMAGE_PLACEHOLDER}                       # "[Image: no text found]" carries no information


class Chunk(BaseModel):
    chunk_index: int = 0
    chunk_type: str                                # text / table / slide / sheet
    content: str
    embed_text: str = ""
    heading_path: list[str] = Field(default_factory=list)
    location: dict = Field(default_factory=dict)
    element_ids: list[str] = Field(default_factory=list)
    split_part: Optional[int] = None               # 1, 2, 3... when one section/table had to be split
    token_count: int = 0


# ---------------- small helpers ----------------
def _title(doc: Document) -> str:
    return (doc.metadata.title or doc.file_name.rsplit(".", 1)[0]).strip()


def _location(elements: list[Element]) -> dict:
    pages = sorted({e.location.page for e in elements if e.location.page})
    slides = sorted({e.location.slide for e in elements if e.location.slide})
    sheets = [e.location.sheet for e in elements if e.location.sheet]
    loc = {}
    if pages:
        loc["pages"] = pages
    if slides:
        loc["slides"] = slides
    if sheets:
        loc["sheet"] = sheets[0]
    return loc


def _same(a: str, b: str) -> bool:
    """'Quarterly HR Report Q3 2026' == 'Quarterly HR Report - Q3 2026' (ignore case and punctuation)."""
    return re.sub(r"[^a-z0-9]", "", a.lower()) == re.sub(r"[^a-z0-9]", "", b.lower())


def _embed_text(title: str, path: list[str], content: str) -> str:
    """Context line + content. The title is not repeated if the path already starts with it."""
    parts = path if path and path[0] == title else [title, *path]
    return " > ".join(parts) + "\n\n" + content


def _element_text(e: Element) -> str:
    return f"Speaker notes: {e.text}" if e.type == T.NOTES else e.text


def _split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _fits(title, path, content) -> bool:
    return count_tokens(_embed_text(title, path, content)) <= settings.chunk_max_tokens


# ---------------- splitting long pieces ----------------
def _split_text(title: str, path: list[str], text: str) -> list[str]:
    """Pack sentences up to the target size; carry the last sentence(s) over as overlap."""
    sentences, pieces, current = _split_sentences(text), [], []
    for s in sentences:
        while not _fits(title, path, s):                       # one giant 'sentence': cut by words
            words = s.split()
            half = max(1, len(words) // 2)
            sentences_left = " ".join(words[half:])
            s = " ".join(words[:half])
            if _fits(title, path, s):
                pieces.append(" ".join(current)) if current else None
                current = []
                pieces.append(s)
                s = sentences_left
        candidate = " ".join(current + [s])
        if current and count_tokens(_embed_text(title, path, candidate)) > settings.chunk_target_tokens:
            pieces.append(" ".join(current))
            overlap, size = [], 0
            for prev in reversed(current):                      # overlap: last sentences, up to N tokens
                size += count_tokens(prev)
                if size > settings.chunk_overlap_tokens:
                    break
                overlap.insert(0, prev)
            current = overlap + [s]
        else:
            current.append(s)
    if current:
        pieces.append(" ".join(current))
    return [p for p in pieces if p.strip()]


def _split_table(title: str, path: list[str], table: str, intro: str = "") -> list[str]:
    """Split a Markdown table by rows; every piece starts with the header + separator line."""
    lines = table.splitlines()
    header, rows = lines[:2], lines[2:]
    prefix = (intro + "\n") if intro else ""
    pieces, current = [], []
    for row in rows:
        candidate = prefix + "\n".join(header + current + [row])
        if current and not _fits(title, path, candidate):
            pieces.append(prefix + "\n".join(header + current))
            current = [row]
        else:
            current.append(row)
    pieces.append(prefix + "\n".join(header + current))
    return pieces


# ---------------- building chunks ----------------
class _Builder:
    def __init__(self, doc: Document):
        self.doc, self.title, self.chunks = doc, _title(doc), []

    def add(self, chunk_type: str, path: list[str], content: str, elements: list[Element], split_part=None):
        content = content.strip()
        if not content:
            return
        self.chunks.append(Chunk(
            chunk_type=chunk_type, content=content, heading_path=list(path) or [self.title],
            location=_location(elements), element_ids=[e.element_id for e in elements], split_part=split_part,
        ))

    def add_text(self, chunk_type, path, content, elements):
        if _fits(self.title, path, content):
            self.add(chunk_type, path, content, elements)
        else:
            for i, piece in enumerate(_split_text(self.title, path, content), start=1):
                self.add(chunk_type, path, piece, elements, split_part=i)

    def add_table(self, path, table: Element, intro: str = "", also: list[Element] = ()):
        """also = elements that belong to this table chunk too (e.g. the sheet-name heading)."""
        content = (intro + "\n" + table.text) if intro else table.text
        if _fits(self.title, path, content):
            self.add("table", path, content, [*also, table])
        else:
            for i, piece in enumerate(_split_table(self.title, path, table.text, intro), start=1):
                self.add("table", path, piece, [*also, table], split_part=i)

    def finish(self) -> list[Chunk]:
        for i, c in enumerate(self.chunks):
            c.chunk_index = i
            c.embed_text = _embed_text(self.title, c.heading_path, c.content)
            c.token_count = count_tokens(c.embed_text)
        return self.chunks


def chunk_document(doc: Document) -> list[Chunk]:
    b = _Builder(doc)
    elements = [e for e in doc.elements if e.type not in SKIP and e.text.strip()]
    if doc.file_type == "pptx":
        _chunk_slides(b, elements)
    elif doc.file_type in ("xlsx", "csv"):
        _chunk_sheets(b, elements)
    else:
        _chunk_sections(b, elements)
    return b.finish()


def _chunk_slides(b: _Builder, elements: list[Element]) -> None:
    by_slide: dict[int, list[Element]] = {}
    for e in elements:
        by_slide.setdefault(e.location.slide or 0, []).append(e)
    for slide, els in by_slide.items():
        heading = next((e.text for e in els if e.type == T.HEADING), f"Slide {slide}")
        content = "\n".join(_element_text(e) for e in els)
        b.add_text("slide", [b.title, heading] if heading != b.title else [b.title], content, els)


def _chunk_sheets(b: _Builder, elements: list[Element]) -> None:
    sheet_heading = None
    for e in elements:
        if e.type == T.HEADING:
            sheet_heading = e
        elif e.type == T.TABLE:
            b.add_table([b.title, f"Sheet: {e.location.sheet}"], e, also=[sheet_heading] if sheet_heading else [])


def _chunk_sections(b: _Builder, elements: list[Element]) -> None:
    """Walk the elements; a heading starts a new section. Tables become their own chunks."""
    stack: list[tuple[int, str]] = []         # [(level, heading text)]
    body: list[Element] = []
    heading_el: Optional[Element] = None

    def path() -> list[str]:
        out = [b.title]
        for _, h in stack:
            if not _same(h, out[-1]) and not _same(h, b.title):
                out.append(h)
        return out

    def flush():
        nonlocal body, heading_el
        texts, text_els = [], []
        lead = [heading_el] if heading_el else []
        for e in body:
            if e.type == T.TABLE:
                if texts:                                     # text before the table -> its own chunk
                    b.add_text("text", path(), "\n".join(_element_text(x) for x in lead + text_els), lead + text_els)
                    texts, text_els, lead = [], [], []
                intro = heading_el.text if heading_el and lead else ""
                b.add_table(path(), e, intro=intro)
                if intro:
                    lead = []                                 # heading used as the table's intro
            else:
                texts.append(e)
                text_els.append(e)
        if text_els:
            b.add_text("text", path(), "\n".join(_element_text(x) for x in lead + text_els), lead + text_els)
        elif lead and not any(e.type == T.TABLE for e in body):
            pass                                              # heading with no body: only part of the path
        body, heading_el = [], None

    for e in elements:
        if e.type == T.HEADING:
            flush()
            level = e.level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, e.text))
            heading_el = e
        else:
            body.append(e)
    flush()
