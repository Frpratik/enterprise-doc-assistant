"""
Automatic checks on the chunks of one document (run every time we chunk).

  no empty chunk                          every chunk has a heading path
  no chunk above the token limit          every table piece starts with its header row
  every element is used:
      body elements (text, tables, OCR)   -> in at least one chunk; in more than one ONLY if it was split
      headings                            -> either in a chunk's text or in a chunk's heading path
Returns a list of problems (empty list = all good).
"""
from collections import Counter

from indexing.chunker import SKIP, Chunk
from ingestion.config import settings
from ingestion.models import Document, ElementType


def check_chunks(doc: Document, chunks: list[Chunk]) -> list[str]:
    problems = []
    for c in chunks:
        where = f"chunk {c.chunk_index}"
        if not c.content.strip():
            problems.append(f"{where}: empty")
        if c.token_count > settings.chunk_max_tokens:
            problems.append(f"{where}: {c.token_count} tokens > limit {settings.chunk_max_tokens}")
        if not c.heading_path:
            problems.append(f"{where}: no heading path")
        if c.chunk_type == "table":
            lines = [l for l in c.content.splitlines() if l.startswith("|")]
            if len(lines) < 2 or not set(lines[1].replace("|", "").strip()) <= {"-", " "}:
                problems.append(f"{where}: table piece without a header row")

    used = Counter(eid for c in chunks for eid in c.element_ids)
    split_ids = {eid for c in chunks if c.split_part for eid in c.element_ids}
    in_paths = {h for c in chunks for h in c.heading_path}
    for e in doc.elements:
        if e.type in SKIP or not e.text.strip():
            continue
        n = used[e.element_id]
        if e.type == ElementType.HEADING:
            if n == 0 and e.text not in in_paths and not any(_close(e.text, h) for h in in_paths):
                problems.append(f"heading {e.element_id} '{e.text[:40]}' is lost")
        elif n == 0:
            problems.append(f"element {e.element_id} ({e.type.value}) is in no chunk")
        elif n > 1 and e.element_id not in split_ids:
            problems.append(f"element {e.element_id} is in {n} chunks but was not split")
    return problems


def _close(a: str, b: str) -> bool:
    import re
    return re.sub(r"[^a-z0-9]", "", a.lower()) == re.sub(r"[^a-z0-9]", "", b.lower())
