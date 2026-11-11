"""
The pipeline: runs every stage, in order, for ONE file.

  gate (intake + safety) -> detect -> parse -> clean -> label -> quality -> save -> log

One bad file never crashes the run: any error inside parsing/cleaning becomes
status FAILED with the error as the reason, and the next file goes on.
"""
import time
from pathlib import Path
from typing import Optional

from ingestion.clean import clean_document
from ingestion.config import settings
from ingestion.detect import classify_pdf, final_label, initial_label
from ingestion.intake import admit
from ingestion.logs import get_logger
from ingestion.manifest import Manifest
from ingestion.models import Document, Status
from ingestion.quality import check_quality
from ingestion.registry import get_parser
from ingestion.store import copy_to_failed, save_output


def process_file(path: Path, manifest: Manifest, move: bool = False) -> Optional[Document]:
    start = time.perf_counter()
    log = get_logger().bind(file=Path(path).name)

    # ---- 1. gate ----
    # inbox files may still be copying -> wait; a file the user points at is already complete
    doc = admit(Path(path), manifest, move=move, wait=move)
    if doc is None:
        log.info("ignored_temp_file")
        return None
    log = log.bind(doc_id=doc.doc_id, version=doc.version)
    if doc.status != Status.QUEUED:                      # failed at the gate, or duplicate
        _finish(doc, start, log, saved=False)
        return doc

    doc.mark(Status.PROCESSING)
    manifest.save(doc)
    original = Path(doc.original_path)

    try:
        # ---- 2. detect ----
        kinds = classify_pdf(original) if doc.file_type == "pdf" else None
        doc.doc_label = initial_label(doc.file_type, kinds)

        # ---- 3. parse ----
        parser = get_parser(doc.file_type)
        doc.parser, doc.parser_version = parser.name, settings.parser_version
        for element in parser.parse(doc, original):
            doc.add(element)

        # ---- 4. clean, 5. final label, 6. quality ----
        clean_document(doc)
        doc.doc_label = final_label(doc)
        check_quality(doc)
    except Exception as e:                               # isolate: this file fails, the run goes on
        doc.mark(Status.FAILED, f"error while processing: {type(e).__name__}: {e}")
        log.exception("processing_error")

    if doc.status == Status.FAILED:
        copy_to_failed(doc)
    _finish(doc, start, log, saved=True)
    manifest.save(doc)
    return doc


def _finish(doc: Document, start: float, log, saved: bool) -> None:
    doc.duration_ms = int((time.perf_counter() - start) * 1000)
    if saved:
        doc.output_path = save_output(doc)
    log.info(
        "document_processed",
        status=doc.status.value, reason=doc.status_reason, file_type=doc.file_type,
        parser=doc.parser, pages=doc.metadata.page_count, elements=len(doc.elements),
        score=doc.quality.score, duration_ms=doc.duration_ms,
    )
