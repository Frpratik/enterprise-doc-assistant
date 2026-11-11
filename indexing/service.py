"""
Phase 1 + Phase 2 together for one file:  process (gate -> parse -> clean -> quality) -> index.
"""
from pathlib import Path

from indexing.doc_store import PgManifest
from indexing.indexer import IndexResult, index_document
from ingestion.models import Document
from ingestion.pipeline import process_file


def ingest_and_index(path: Path, manifest: PgManifest | None = None, move: bool = False,
                     embedder=None) -> tuple[Document | None, IndexResult | None]:
    doc = process_file(Path(path), manifest or PgManifest(), move=move)
    if doc is None:
        return None, None
    return doc, index_document(doc, embedder)
