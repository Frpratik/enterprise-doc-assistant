"""
Phase 1 + Phase 2 together for one file:  process (gate -> parse -> clean -> quality) -> index.
"""
from pathlib import Path

from indexing.db import connect
from indexing.doc_store import PgManifest
from indexing.indexer import IndexResult, index_document
from ingestion.models import Document, Status
from ingestion.pipeline import process_file


def ingest_and_index(path: Path, manifest: PgManifest | None = None, move: bool = False,
                     embedder=None) -> tuple[Document | None, IndexResult | None]:
    doc = process_file(Path(path), manifest or PgManifest(), move=move)
    if doc is None:
        return None, None
    if doc.status == Status.SKIPPED_DUPLICATE:
        pending = _unindexed_original(doc)
        if pending:                              # same file as one that was processed but never indexed
            return pending, index_document(pending, embedder)
    return doc, index_document(doc, embedder)


def _unindexed_original(dup: Document) -> Document | None:
    """
    A duplicate normally means 'nothing to do'. But if the earlier copy finished processing and then
    indexing failed or never ran (worker crash, missing migration), skipping would leave it
    unsearchable forever. In that case, load its saved result so it can be indexed now.
    """
    with connect() as conn:
        row = conn.execute("SELECT output_path FROM documents WHERE doc_id = %s AND version = %s "
                           "AND status = 'done' AND indexed_at IS NULL AND deleted_at IS NULL",
                           (dup.doc_id, dup.version)).fetchone()
    if not row or not row[0] or not Path(row[0]).exists():
        return None
    return Document.model_validate_json(Path(row[0]).read_text(encoding="utf-8"))
