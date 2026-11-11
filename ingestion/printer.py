"""Readable console view of a processed document (plain ASCII, safe on any Windows console)."""
from collections import defaultdict

from ingestion.models import Document, ElementType, Status

LINE = "=" * 78
THIN = "-" * 78
STATUS_TAG = {
    Status.DONE: "[OK] DONE",
    Status.NEEDS_REVIEW: "[!] NEEDS REVIEW",
    Status.FAILED: "[X] FAILED",
    Status.SKIPPED_DUPLICATE: "[=] SKIPPED (duplicate)",
}
NAMES = {
    ElementType.HEADING: "Heading", ElementType.PARAGRAPH: "Paragraph", ElementType.LIST_ITEM: "List item",
    ElementType.TABLE: "Table", ElementType.IMAGE_OCR: "Image OCR", ElementType.IMAGE_PLACEHOLDER: "Image",
    ElementType.NOTES: "Speaker notes",
}


def print_document(doc: Document, max_chars: int = 0, index=None) -> None:
    m = doc.metadata
    print(LINE)
    print(f"FILE   : {doc.file_name}")
    unit = {"pptx": "slides", "xlsx": "sheets", "csv": "sheet"}.get(doc.file_type, "pages")
    details = [doc.file_type or "?", f"{m.page_count} {unit}" if m.page_count else None,
               f"lang={m.language}" if m.language else None, f"label={doc.doc_label}" if doc.doc_label else None]
    print(f"TYPE   : {', '.join(d for d in details if d)}")
    print(f"ID     : {doc.doc_id}  v{doc.version}   parser={doc.parser or '-'} {doc.parser_version or ''}"
          f"   time={doc.duration_ms or 0} ms")
    if m.title or m.author:
        print(f"META   : title={m.title!r} author={m.author!r} created={m.created}")
    score = f"   score={doc.quality.score}" if doc.quality.score is not None else ""
    print(f"STATUS : {STATUS_TAG.get(doc.status, doc.status.value)}{score}")
    if doc.status_reason:
        print(f"REASON : {doc.status_reason}")

    if doc.elements:
        print(THIN)
        counter = defaultdict(int)
        for e in doc.elements:
            if e.type == ElementType.HEADING:
                label = f"[{e.location.label()} - Heading H{e.level or 1}"
            else:
                counter[(e.location.label(), e.type)] += 1
                label = f"[{e.location.label()} - {NAMES[e.type]} {counter[(e.location.label(), e.type)]}"
            if e.ocr_confidence is not None:
                label += f" | OCR {e.ocr_confidence:.2f}"
            label += "]"
            text = e.text if not max_chars or len(e.text) <= max_chars else e.text[:max_chars] + " ..."
            sep = "\n" if "\n" in text else " "
            print(f"{label}{sep}{text}")

    if doc.quality.checks:
        print(THIN)
        main = {k: v for k, v in doc.quality.checks.items() if not k.startswith("clean_")}
        clean = {k.replace("clean_", ""): v for k, v in doc.quality.checks.items() if k.startswith("clean_") and v}
        print("QUALITY: " + ", ".join(f"{k}={v}" for k, v in main.items()))
        if clean:
            print("CLEANED: " + ", ".join(f"{k}={v}" for k, v in clean.items()))
        counts = doc.counts()
        if counts:
            print("FOUND  : " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    if doc.output_path:
        print(f"SAVED  : {doc.output_path}")
    if index is not None:
        if index.indexed:
            replaced = f", replaced v{index.replaced_version}" if index.replaced_version else ""
            print(f"INDEXED: {index.chunks} chunks searchable ({index.embeddings_new} new embeddings, "
                  f"{index.embeddings_cached} reused{replaced}) in {index.duration_ms} ms")
        else:
            print(f"INDEXED: no - {index.reason}")
    print(LINE + "\n")


def print_summary(docs: list[Document]) -> None:
    if not docs:
        print("No files processed.")
        return
    print(f"\nSUMMARY ({len(docs)} files)")
    print(f"{'FILE':34} {'STATUS':18} {'SCORE':>5}  REASON")
    for d in docs:
        score = "" if d.quality.score is None else f"{d.quality.score:.2f}"
        reason = (d.status_reason or "")[:70]
        print(f"{d.file_name[:34]:34} {d.status.value:18} {score:>5}  {reason}")
    totals = defaultdict(int)
    for d in docs:
        totals[d.status.value] += 1
    print("TOTAL: " + ", ".join(f"{v} {k}" for k, v in totals.items()))
