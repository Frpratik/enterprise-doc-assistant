"""
Keep the index matching a folder, plus delete / cleanup / approve.

SYNC compares the folder with the documents table and works out a plan:
  new        file not seen before                     -> ingest + index
  changed    same name, different content (hash)      -> new version, only changed sections re-embedded
  unchanged  same name, same content                  -> skip (costs nothing)
  renamed    a "new" file whose content equals a file that disappeared -> just rename it (no re-index)
  deleted    file is gone from the folder             -> soft delete: hidden from search at once,
                                                         rows kept until cleanup (audit / undo)
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from indexing.db import connect
from indexing.doc_store import PgManifest
from indexing.indexer import IndexResult, index_document
from indexing.service import ingest_and_index
from ingestion.config import settings
from ingestion.intake import is_temp_file
from ingestion.logs import get_logger
from ingestion.models import Document, Status
from ingestion.safety import sha256


@dataclass
class SyncPlan:
    new: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    renamed: list[tuple[str, Path]] = field(default_factory=list)      # (doc_id, new path)
    deleted: list[tuple[str, str]] = field(default_factory=list)       # (doc_id, old file name)


def _tracked(folder: Path) -> dict[str, dict]:
    """Latest live version of every document that came from this folder, by file name."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT ON (doc_id) doc_id, version, file_name, hash, source_path
               FROM documents WHERE deleted_at IS NULL AND status <> 'failed'
               ORDER BY doc_id, version DESC""").fetchall()
    out = {}
    for doc_id, version, name, h, src in rows:
        if src and Path(src).parent.resolve() == folder.resolve():
            out[name.lower()] = {"doc_id": doc_id, "version": version, "file_name": name, "hash": h}
    return out


def plan_sync(folder: Path) -> SyncPlan:
    folder = Path(folder)
    files = sorted(p for p in folder.iterdir() if p.is_file() and not is_temp_file(p) and p.name != ".gitkeep")
    tracked = _tracked(folder)
    plan = SyncPlan()
    seen = set()
    for f in files:
        rec = tracked.get(f.name.lower())
        if rec is None:
            plan.new.append(f)
        else:
            seen.add(f.name.lower())
            (plan.unchanged if sha256(f) == rec["hash"] else plan.changed).append(f)
    gone = {rec["hash"]: rec for name, rec in tracked.items() if name not in seen}
    for f in list(plan.new):                                  # a "new" file with a vanished file's content = rename
        rec = gone.pop(sha256(f), None)
        if rec:
            plan.new.remove(f)
            plan.renamed.append((rec["doc_id"], f))
    plan.deleted = [(rec["doc_id"], rec["file_name"]) for rec in gone.values()]
    return plan


def run_sync(folder: Path, dry_run: bool = False, embedder=None, on_file=None) -> SyncPlan:
    plan = plan_sync(folder)
    if dry_run:
        return plan
    log = get_logger().bind(folder=str(folder))
    manifest = PgManifest()
    for f in plan.new + plan.changed:
        doc, idx = ingest_and_index(f, manifest, move=False, embedder=embedder)
        if on_file:
            on_file(f, doc, idx)
    for doc_id, f in plan.renamed:
        rename_document(doc_id, f)
    for doc_id, _ in plan.deleted:
        delete_document(doc_id)
    log.info("sync_done", new=len(plan.new), changed=len(plan.changed), unchanged=len(plan.unchanged),
             renamed=len(plan.renamed), deleted=len(plan.deleted))
    return plan


def rename_document(doc_id: str, new_path: Path) -> None:
    with connect() as conn:
        conn.execute("UPDATE documents SET file_name = %s, source_path = %s, updated_at = now() WHERE doc_id = %s",
                     (new_path.name, str(new_path), doc_id))
        conn.execute("UPDATE chunks SET metadata = jsonb_set(metadata, '{file_name}', to_jsonb(%s::text)) "
                     "WHERE doc_id = %s", (new_path.name, doc_id))


def delete_document(doc_id: str) -> bool:
    """Soft delete: gone from search immediately, rows kept until cleanup."""
    with connect() as conn:
        n = conn.execute("UPDATE documents SET deleted_at = now(), is_active = false, updated_at = now() "
                         "WHERE doc_id = %s AND deleted_at IS NULL", (doc_id,)).rowcount
        conn.execute("UPDATE chunks SET is_active = false WHERE doc_id = %s", (doc_id,))
    get_logger().info("document_deleted", doc_id=doc_id)
    return n > 0


def cleanup(days: int | None = None) -> dict:
    """Hard delete what's been soft-deleted (or replaced by a newer version) for more than N days,
    then remove embeddings no chunk uses any more."""
    days = settings.keep_deleted_days if days is None else days
    with connect() as conn:
        deleted_docs = conn.execute(
            "DELETE FROM documents WHERE deleted_at < now() - make_interval(days => %s)", (days,)).rowcount
        old_versions = conn.execute(
            """DELETE FROM documents d WHERE NOT d.is_active AND d.deleted_at IS NULL
                 AND d.updated_at < now() - make_interval(days => %s)
                 AND EXISTS (SELECT 1 FROM documents n WHERE n.doc_id = d.doc_id AND n.version > d.version)""",
            (days,)).rowcount
        orphans = conn.execute(
            "DELETE FROM embeddings e WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.embed_hash = e.embed_hash)"
        ).rowcount
    out = {"deleted_documents": deleted_docs, "old_versions": old_versions, "unused_embeddings": orphans}
    get_logger().info("cleanup_done", days=days, **out)
    return out


def approve_document(doc_id: str, embedder=None) -> IndexResult:
    """A person checked a needs_review document: mark it done and make it searchable."""
    with connect() as conn:
        row = conn.execute("SELECT version, output_path, status FROM documents WHERE doc_id = %s "
                           "AND deleted_at IS NULL ORDER BY version DESC LIMIT 1", (doc_id,)).fetchone()
    if not row:
        raise ValueError(f"no document {doc_id}")
    version, output_path, status = row
    if status != Status.NEEDS_REVIEW.value:
        raise ValueError(f"{doc_id} v{version} is '{status}', only needs_review documents can be approved")
    doc = Document.model_validate_json(Path(output_path).read_text(encoding="utf-8"))
    doc.mark(Status.DONE, f"approved by reviewer (was: {doc.status_reason})")
    PgManifest().save(doc)
    Path(output_path).write_text(doc.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    return index_document(doc, embedder)


def list_documents() -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """SELECT doc_id, version, file_name, status, is_active, chunk_count,
                      to_char(deleted_at, 'YYYY-MM-DD') AS deleted
               FROM documents ORDER BY file_name, version""").fetchall()
