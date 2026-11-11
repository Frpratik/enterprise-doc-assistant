"""
Indexer = put one processed document version into the search index, safely.

  1. CHUNK    chunk_document() + check_chunks()        (refuse to index broken chunks)
  2. EMBED    embed_with_cache()                        (only never-seen texts go to the model)
  3. WRITE    insert the chunks as INACTIVE             (transaction 1)
  4. SWAP     old version -> inactive, this one -> active, in ONE transaction   (transaction 2)

Why two steps? If anything crashes between WRITE and SWAP, search still sees the complete
OLD version; the half-written new rows are inactive and simply replaced on the next run.
Search never sees a document half-updated, and never sees two versions mixed.
"""
import json
import re
import time
from dataclasses import dataclass, field

from indexing.chunk_checks import check_chunks
from indexing.chunker import chunk_document
from indexing.db import connect
from indexing.embeddings import Embedder, embed_with_cache, get_embedder, text_hash
from ingestion.config import settings
from ingestion.logs import get_logger
from ingestion.models import Document, Status


@dataclass
class IndexResult:
    doc_id: str
    version: int
    indexed: bool
    reason: str = ""
    chunks: int = 0
    embeddings_new: int = 0
    embeddings_cached: int = 0
    duration_ms: int = 0
    replaced_version: int | None = None
    stats: dict = field(default_factory=dict)


class ChunkProblem(Exception):
    pass


def should_index(doc: Document) -> tuple[bool, str]:
    if doc.status == Status.DONE:
        return True, ""
    if doc.status == Status.NEEDS_REVIEW and settings.index_needs_review:
        return True, ""
    if doc.status == Status.NEEDS_REVIEW:
        return False, "needs review: waiting for a person to approve it before it becomes searchable"
    return False, f"status is '{doc.status.value}'"


def ensure_vector_index(embedder: Embedder) -> None:
    """One HNSW index per embedding model (each model has its own vector size)."""
    slug = re.sub(r"[^a-z0-9]+", "_", embedder.name.lower()).strip("_")[:40]
    with connect() as conn:
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS embeddings_hnsw_{slug} ON embeddings "
            f"USING hnsw ((embedding::vector({embedder.dims})) vector_cosine_ops) "
            f"WHERE embed_model = '{embedder.name}'")


def index_document(doc: Document, embedder: Embedder | None = None) -> IndexResult:
    start = time.perf_counter()
    log = get_logger().bind(doc_id=doc.doc_id, version=doc.version, file=doc.file_name)
    ok, reason = should_index(doc)
    if not ok:
        log.info("index_skipped", reason=reason)
        return IndexResult(doc.doc_id, doc.version, indexed=False, reason=reason)

    embedder = embedder or get_embedder()
    try:
        # ---- 1. chunk + check ----
        chunks = chunk_document(doc)
        problems = check_chunks(doc, chunks)
        if problems:
            raise ChunkProblem("; ".join(problems[:3]))
        if not chunks:
            raise ChunkProblem("no chunks produced")

        ensure_vector_index(embedder)

        # ---- 2 + 3. embed (cached) and WRITE as inactive  [transaction 1] ----
        with connect() as conn:
            _, emb_stats = embed_with_cache(conn, [c.embed_text for c in chunks], embedder)
            conn.execute("DELETE FROM chunks WHERE doc_id = %s AND version = %s", (doc.doc_id, doc.version))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO chunks (chunk_id, doc_id, version, chunk_index, chunk_type, content, embed_text,
                           embed_hash, token_count, heading_path, location, metadata, is_active)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false)""",
                    [(f"{doc.doc_id}_v{doc.version}_{c.chunk_index:04d}", doc.doc_id, doc.version, c.chunk_index,
                      c.chunk_type, c.content, c.embed_text, text_hash(c.embed_text), c.token_count,
                      " > ".join(c.heading_path), json.dumps(c.location),
                      json.dumps({"file_name": doc.file_name, "title": doc.metadata.title, "doc_label": doc.doc_label,
                                  "language": doc.metadata.language, "element_ids": c.element_ids,
                                  "split_part": c.split_part, "embed_model": embedder.name}))
                     for c in chunks])

        # ---- 4. SWAP  [transaction 2] ----
        replaced = _swap(doc, len(chunks), embedder.name)

    except Exception as e:
        _record_error(doc, f"{type(e).__name__}: {e}")
        log.exception("index_failed")
        return IndexResult(doc.doc_id, doc.version, indexed=False, reason=f"indexing failed: {e}")

    result = IndexResult(doc.doc_id, doc.version, indexed=True, chunks=len(chunks),
                         embeddings_new=emb_stats["new"], embeddings_cached=emb_stats["cached"],
                         duration_ms=int((time.perf_counter() - start) * 1000), replaced_version=replaced)
    log.info("document_indexed", chunks=result.chunks, embeddings_new=result.embeddings_new,
             embeddings_cached=result.embeddings_cached, replaced_version=replaced, duration_ms=result.duration_ms)
    return result


def _swap(doc: Document, n_chunks: int, model: str) -> int | None:
    """All in ONE transaction: either everything below happens, or nothing does."""
    with connect() as conn:
        prev = conn.execute("SELECT version FROM documents WHERE doc_id = %s AND is_active AND version <> %s",
                            (doc.doc_id, doc.version)).fetchone()
        conn.execute("UPDATE chunks SET is_active = (version = %s) WHERE doc_id = %s", (doc.version, doc.doc_id))
        conn.execute("UPDATE documents SET is_active = (version = %s), updated_at = now() WHERE doc_id = %s",
                     (doc.version, doc.doc_id))
        conn.execute("UPDATE documents SET indexed_at = now(), chunk_count = %s, embed_model = %s, index_error = NULL "
                     "WHERE doc_id = %s AND version = %s", (n_chunks, model, doc.doc_id, doc.version))
    return prev[0] if prev else None


def _record_error(doc: Document, error: str) -> None:
    try:
        with connect() as conn:
            conn.execute("UPDATE documents SET index_error = %s, updated_at = now() WHERE doc_id = %s AND version = %s",
                         (error[:500], doc.doc_id, doc.version))
    except Exception:
        pass
