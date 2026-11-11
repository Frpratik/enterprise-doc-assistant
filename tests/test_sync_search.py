"""Phase 2 Stage 5 tests: sync (new / unchanged / changed / renamed / deleted), cleanup, approve, search."""
import numpy as np
import pytest

from indexing import sync
from indexing.db import connect
from indexing.embeddings import Embedder, get_embedder
from indexing.search import search
from ingestion.config import settings


class FakeEmbedder(Embedder):
    """Fast stand-in: vectors from character counts (similar texts -> similar vectors)."""
    name, dims = "fake-model", 26

    def __init__(self):
        self.embedded = 0

    def embed_documents(self, texts):
        self.embedded += len(texts)
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


DOCS = {
    "leave.md": "# Leave\n\n## Sick Leave\nEmployees get 12 sick leaves per year.\n",
    "travel.md": "# Travel\n\n## Hotels\nHotel stays up to Rs 4,000 per night.\n",
    "it.md": "# IT\n\n## Passwords\nChange passwords every 90 days.\n",
}


@pytest.fixture
def folder(db_url, temp_data):
    src = temp_data / "source"
    src.mkdir()
    for name, text in DOCS.items():
        (src / name).write_text(text)
    return src


@pytest.fixture
def fake():
    return FakeEmbedder()


def searchable_files():
    with connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT d.file_name FROM chunks c JOIN documents d USING (doc_id, version) "
            "WHERE c.is_active AND d.deleted_at IS NULL")}


def test_first_sync_adds_everything_second_sync_does_nothing(folder, fake):
    plan = sync.run_sync(folder, embedder=fake)
    assert len(plan.new) == 3 and searchable_files() == set(DOCS)
    before = fake.embedded
    plan2 = sync.run_sync(folder, embedder=fake)
    assert len(plan2.unchanged) == 3 and not (plan2.new or plan2.changed or plan2.deleted)
    assert fake.embedded == before                                   # nothing re-embedded


def test_dry_run_changes_nothing(folder, fake):
    plan = sync.run_sync(folder, dry_run=True, embedder=fake)
    assert len(plan.new) == 3 and fake.embedded == 0 and searchable_files() == set()


def test_changed_file_becomes_new_version(folder, fake):
    sync.run_sync(folder, embedder=fake)
    (folder / "travel.md").write_text(DOCS["travel.md"].replace("Rs 4,000", "Rs 5,500"))
    plan = sync.run_sync(folder, embedder=fake)
    assert [p.name for p in plan.changed] == ["travel.md"] and len(plan.unchanged) == 2
    with connect() as conn:
        assert conn.execute("SELECT max(version) FROM documents WHERE file_name='travel.md'").fetchone()[0] == 2


def test_deleted_file_disappears_from_search(folder, fake):
    sync.run_sync(folder, embedder=fake)
    (folder / "it.md").unlink()
    plan = sync.run_sync(folder, embedder=fake)
    assert [n for _, n in plan.deleted] == ["it.md"]
    assert searchable_files() == {"leave.md", "travel.md"}
    assert all(h.file_name != "it.md" for h in search("change passwords", k=10, embedder=fake))
    with connect() as conn:                                         # soft delete: rows still there
        assert conn.execute("SELECT deleted_at IS NOT NULL FROM documents WHERE file_name='it.md'").fetchone()[0]


def test_renamed_file_is_not_reindexed(folder, fake):
    sync.run_sync(folder, embedder=fake)
    before = fake.embedded
    (folder / "leave.md").rename(folder / "leave_policy_2026.md")
    plan = sync.run_sync(folder, embedder=fake)
    assert len(plan.renamed) == 1 and not plan.deleted and not plan.new
    assert fake.embedded == before                                   # zero re-embedding
    assert "leave_policy_2026.md" in searchable_files() and "leave.md" not in searchable_files()


def test_cleanup_removes_old_deleted_data(folder, fake):
    sync.run_sync(folder, embedder=fake)
    (folder / "it.md").unlink()
    sync.run_sync(folder, embedder=fake)
    assert sync.cleanup(days=30)["deleted_documents"] == 0           # too recent: kept
    with connect() as conn:
        conn.execute("UPDATE documents SET deleted_at = now() - interval '40 days' WHERE file_name = 'it.md'")
    result = sync.cleanup(days=30)
    assert result["deleted_documents"] == 1 and result["unused_embeddings"] >= 1
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM documents WHERE file_name='it.md'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM chunks c LEFT JOIN documents d USING (doc_id, version) "
                            "WHERE d.doc_id IS NULL").fetchone()[0] == 0   # chunks removed with the document


def test_approve_makes_needs_review_searchable(db_url, temp_data, samples, fake):
    src = temp_data / "source"
    src.mkdir()
    (src / "gift.pdf").write_bytes((samples / "good" / "policy_with_blank_page.pdf").read_bytes())
    sync.run_sync(src, embedder=fake)
    assert searchable_files() == set()                               # needs_review: not searchable yet
    with connect() as conn:
        doc_id = conn.execute("SELECT doc_id FROM documents WHERE file_name='gift.pdf'").fetchone()[0]
    result = sync.approve_document(doc_id, embedder=fake)
    assert result.indexed and searchable_files() == {"gift.pdf"}
    with connect() as conn:
        status, reason = conn.execute("SELECT status, status_reason FROM documents WHERE doc_id=%s",
                                      (doc_id,)).fetchone()
    assert status == "done" and "approved by reviewer" in reason


def test_search_returns_best_first_and_respects_k(folder, fake):
    sync.run_sync(folder, embedder=fake)
    hits = search("passwords", k=2, embedder=fake)
    assert len(hits) == 2 and hits[0].score >= hits[1].score
    assert hits[0].heading_path and hits[0].file_name in DOCS


@pytest.mark.skipif(not any(settings.models_dir.rglob("tokenizer.json")), reason="model not downloaded")
def test_search_with_real_model(db_url, temp_data):
    src = temp_data / "source"
    src.mkdir()
    for name, text in DOCS.items():
        (src / name).write_text(text)
    model = get_embedder("local")
    sync.run_sync(src, embedder=model)
    for question, expected in [("how many days of sick leave do I get", "leave.md"),
                               ("what is the hotel limit per night", "travel.md"),
                               ("how often do I change my password", "it.md")]:
        assert search(question, k=1, embedder=model)[0].file_name == expected, question
