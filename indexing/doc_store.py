"""
The manifest, now in Postgres (the `documents` table) instead of data/manifest.json.

Same 3 methods as the JSON Manifest from Phase 1, so intake.admit() works with either:
  find_by_hash(hash)        same bytes already ingested?   -> duplicate
  latest_by_name(name)      newest version with this name   -> next version number
  save(doc)                 insert or update this version's row
"""
from typing import Optional

from indexing.db import connect
from ingestion.models import Document

ACTIVE_STATUSES = ("queued", "processing", "done", "needs_review")
COLUMNS = "doc_id, version, file_name, hash, status, status_reason"


def _row(r) -> dict:
    return dict(zip(["doc_id", "version", "file_name", "hash", "status", "status_reason"], r))


class PgManifest:
    def find_by_hash(self, file_hash: str) -> Optional[dict]:
        with connect() as conn:
            r = conn.execute(
                f"SELECT {COLUMNS} FROM documents WHERE hash = %s AND status = ANY(%s) AND deleted_at IS NULL "
                "ORDER BY version DESC LIMIT 1", (file_hash, list(ACTIVE_STATUSES))).fetchone()
        return _row(r) if r else None

    def latest_by_name(self, file_name: str) -> Optional[dict]:
        with connect() as conn:
            r = conn.execute(
                f"SELECT {COLUMNS} FROM documents WHERE lower(file_name) = lower(%s) AND status = ANY(%s) "
                "AND deleted_at IS NULL ORDER BY version DESC LIMIT 1", (file_name, list(ACTIVE_STATUSES))).fetchone()
        return _row(r) if r else None

    def save(self, doc: Document) -> None:
        m = doc.metadata
        with connect() as conn:
            conn.execute(
                """INSERT INTO documents (doc_id, version, file_name, file_type, hash, status, status_reason,
                       doc_label, language, title, page_count, quality_score, source_path, original_path, output_path)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (doc_id, version) DO UPDATE SET
                       status = EXCLUDED.status, status_reason = EXCLUDED.status_reason,
                       doc_label = EXCLUDED.doc_label, language = EXCLUDED.language, title = EXCLUDED.title,
                       page_count = EXCLUDED.page_count, quality_score = EXCLUDED.quality_score,
                       original_path = EXCLUDED.original_path, output_path = EXCLUDED.output_path,
                       updated_at = now()""",
                (doc.doc_id, doc.version, doc.file_name, doc.file_type, doc.hash, doc.status.value, doc.status_reason,
                 doc.doc_label, m.language, m.title, m.page_count, doc.quality.score, doc.source_path,
                 doc.original_path, doc.output_path))
