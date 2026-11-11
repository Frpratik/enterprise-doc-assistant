"""Phase 2 Stages 3-4 tests: documents table, indexing, versions, atomic swap, crash safety."""
import numpy as np
import pytest

from indexing import indexer
from indexing.db import connect
from indexing.doc_store import PgManifest
from indexing.embeddings import Embedder
from indexing.indexer import index_document
from indexing.service import ingest_and_index
from ingestion.config import settings
from ingestion.intake import admit
from ingestion.models import Status

POLICY_V1 = """# Travel Policy

## Booking
Flights must be booked through the travel desk at least 7 days in advance.

## Hotels
Hotel stays are allowed up to Rs 4,000 per night in metro cities.

## Claims
Submit travel claims within 15 days of returning.
"""
POLICY_V2 = POLICY_V1.replace("Rs 4,000 per night", "Rs 5,500 per night")     # HR edits ONE section


class FakeEmbedder(Embedder):
    """Fast, predictable stand-in for the real model; counts how many texts it embedded."""
    name, dims = "fake-model", 8

    def __init__(self):
        self.embedded = 0

    def embed_documents(self, texts):
        self.embedded += len(texts)
        out = []
        for t in texts:
            v = np.zeros(8, dtype=np.float32)
            for i, ch in enumerate(t.encode()[:400]):
                v[i % 8] += ch
            out.append(v / np.linalg.norm(v))
        return out


@pytest.fixture
def env(db_url, temp_data):
    return {"manifest": PgManifest(), "embedder": FakeEmbedder(), "dir": temp_data}


def active_chunks(doc_id):
    with connect() as conn:
        return conn.execute("SELECT version, content FROM chunks WHERE doc_id = %s AND is_active ORDER BY chunk_index",
                            (doc_id,)).fetchall()


def test_new_document_is_indexed_and_searchable(env, samples):
    doc, res = ingest_and_index(samples / "good" / "leave_policy.pdf", env["manifest"], embedder=env["embedder"])
    assert doc.status == Status.DONE and res.indexed
    assert res.chunks == 5 and res.embeddings_new == 5 and res.embeddings_cached == 0
    rows = active_chunks(doc.doc_id)
    assert len(rows) == 5 and any("12 sick leaves" in c for _, c in rows)
    with connect() as conn:
        is_active, chunk_count, model = conn.execute(
            "SELECT is_active, chunk_count, embed_model FROM documents WHERE doc_id = %s", (doc.doc_id,)).fetchone()
    assert is_active and chunk_count == 5 and model == "fake-model"


def test_duplicate_file_is_not_indexed_twice(env, samples):
    ingest_and_index(samples / "good" / "leave_policy.pdf", env["manifest"], embedder=env["embedder"])
    doc, res = ingest_and_index(samples / "bad" / "leave_policy_copy.pdf", env["manifest"], embedder=env["embedder"])
    assert doc.status == Status.SKIPPED_DUPLICATE and not res.indexed
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 5


def test_new_version_reembeds_only_the_changed_section(env):
    f = env["dir"] / "travel_policy.md"
    f.write_text(POLICY_V1)
    v1, r1 = ingest_and_index(f, env["manifest"], embedder=env["embedder"])
    f.write_text(POLICY_V2)
    v2, r2 = ingest_and_index(f, env["manifest"], embedder=env["embedder"])

    assert (v1.doc_id, v1.version) == (v2.doc_id, 1) and v2.version == 2
    assert r1.embeddings_new == 3
    assert r2.embeddings_new == 1 and r2.embeddings_cached == 2        # only "Hotels" was re-embedded
    assert r2.replaced_version == 1
    rows = active_chunks(v1.doc_id)
    assert {v for v, _ in rows} == {2}                                  # search sees ONLY version 2
    assert any("Rs 5,500" in c for _, c in rows) and not any("Rs 4,000" in c for _, c in rows)
    with connect() as conn:
        versions = conn.execute("SELECT version, is_active FROM documents WHERE doc_id = %s ORDER BY version",
                                (v1.doc_id,)).fetchall()
    assert versions == [(1, False), (2, True)]                          # old version kept for history


def test_crash_between_write_and_swap_keeps_old_version(env, monkeypatch):
    f = env["dir"] / "travel_policy.md"
    f.write_text(POLICY_V1)
    v1, _ = ingest_and_index(f, env["manifest"], embedder=env["embedder"])

    def crash(*a, **k):
        raise RuntimeError("power cut during swap")

    # only the crash is patched here (and undone after), the test-database settings stay in place
    with monkeypatch.context() as mp:
        mp.setattr(indexer, "_swap", crash)
        f.write_text(POLICY_V2)
        v2, r2 = ingest_and_index(f, env["manifest"], embedder=env["embedder"])
    assert not r2.indexed and "power cut" in r2.reason
    rows = active_chunks(v1.doc_id)
    assert {v for v, _ in rows} == {1} and any("Rs 4,000" in c for _, c in rows)   # old version still served
    with connect() as conn:
        assert "power cut" in conn.execute("SELECT index_error FROM documents WHERE doc_id=%s AND version=2",
                                           (v1.doc_id,)).fetchone()[0]

    again = index_document(v2, env["embedder"])                         # power is back: re-run is safe
    assert again.indexed and again.embeddings_new == 0                 # embeddings were already saved
    assert {v for v, _ in active_chunks(v1.doc_id)} == {2}


def test_reindexing_same_version_is_safe(env, samples):
    doc, _ = ingest_and_index(samples / "good" / "it_policy.txt", env["manifest"], embedder=env["embedder"])
    again = index_document(doc, env["embedder"])
    assert again.indexed and again.embeddings_new == 0
    assert len(active_chunks(doc.doc_id)) == again.chunks             # no duplicate chunks


def test_needs_review_waits_for_approval(env, samples, monkeypatch):
    doc, res = ingest_and_index(samples / "good" / "policy_with_blank_page.pdf", env["manifest"],
                                embedder=env["embedder"])
    assert doc.status == Status.NEEDS_REVIEW and not res.indexed and "approve" in res.reason
    assert active_chunks(doc.doc_id) == []
    monkeypatch.setattr(settings, "index_needs_review", True)          # e.g. after a person approves it
    assert index_document(doc, env["embedder"]).indexed


def test_failed_files_are_recorded_in_documents_table(env, samples):
    doc, res = ingest_and_index(samples / "bad" / "password_protected.pdf", env["manifest"],
                                embedder=env["embedder"])
    assert doc.status == Status.FAILED and not res.indexed
    with connect() as conn:
        status, reason = conn.execute("SELECT status, status_reason FROM documents WHERE doc_id = %s",
                                      (doc.doc_id,)).fetchone()
    assert status == "failed" and "password" in reason


def test_pg_manifest_finds_duplicates_and_versions(env, samples):
    m = env["manifest"]
    first = admit(samples / "good" / "leave_policy.pdf", m, wait=False)
    dup = admit(samples / "bad" / "leave_policy_copy.pdf", m, wait=False)
    assert first.status == Status.QUEUED and dup.status == Status.SKIPPED_DUPLICATE and dup.doc_id == first.doc_id


def test_vector_index_created_per_model(env, samples):
    ingest_and_index(samples / "good" / "it_policy.txt", env["manifest"], embedder=env["embedder"])
    with connect() as conn:
        names = [r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'embeddings'")]
    assert "embeddings_hnsw_fake_model" in names
