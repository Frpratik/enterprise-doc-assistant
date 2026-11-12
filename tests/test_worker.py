"""Phase 2 Stage 7 tests: background jobs (run in 'eager' mode: same process, no Redis needed)."""
import numpy as np
import psycopg
import pytest
from celery.exceptions import Retry

from indexing import indexer, service, worker
from indexing.db import connect
from indexing.embeddings import Embedder


class FakeEmbedder(Embedder):
    name, dims = "fake-model", 26

    def embed_documents(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


@pytest.fixture
def eager(db_url, temp_data, monkeypatch):
    """Run tasks in this process. The result backend is swapped for memory: tests never need Redis."""
    conf = worker.app.conf
    saved = {k: conf.get(k) for k in ("task_always_eager", "task_eager_propagates")}
    conf.task_always_eager = True
    conf.task_eager_propagates = True
    monkeypatch.setattr(indexer, "get_embedder", lambda *a, **k: FakeEmbedder())
    yield temp_data
    conf.update(saved)


def test_ingest_job_processes_and_indexes(eager, samples):
    result = worker.ingest_file.delay(str(samples / "good" / "leave_policy.pdf")).get()
    assert result["status"] == "done" and result["indexed"] and result["chunks"] == 5
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM chunks WHERE is_active").fetchone()[0] == 5


def test_same_job_twice_is_safe(eager, samples):
    worker.ingest_file.delay(str(samples / "good" / "it_policy.txt")).get()
    again = worker.ingest_file.delay(str(samples / "good" / "it_policy.txt")).get()
    assert again["status"] == "skipped_duplicate" and not again["indexed"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_temporary_database_error_asks_for_retry(eager, samples, monkeypatch):
    calls = {"n": 0}
    real = service.ingest_and_index

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("database restarting")
        return real(*a, **k)

    monkeypatch.setattr(service, "ingest_and_index", flaky)
    # A temporary error must make the task ask for a retry (eager mode does not re-run it;
    # the real retry is checked against the live worker).
    with pytest.raises(Retry):
        worker.ingest_file.delay(str(samples / "good" / "it_policy.txt"))
    assert calls["n"] == 1


def test_broken_file_is_not_retried(eager, samples, monkeypatch):
    calls = {"n": 0}
    real = service.ingest_and_index

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(service, "ingest_and_index", counting)
    result = worker.ingest_file.delay(str(samples / "bad" / "corrupted_report.pdf")).get()
    assert calls["n"] == 1 and result["status"] == "failed" and "corrupted" in result["reason"]


def test_sync_job(eager):
    src = eager / "source"
    src.mkdir()
    (src / "a.md").write_text("# A\n\n## Leave\nEmployees get 12 sick leaves.\n")
    (src / "b.md").write_text("# B\n\n## IT\nChange passwords every 90 days.\n")
    result = worker.sync_folder.delay(str(src)).get()
    assert result["new"] == 2 and result["deleted"] == 0


def test_reliability_settings():
    conf = worker.app.conf
    assert conf.task_acks_late and conf.worker_prefetch_multiplier == 1 and conf.task_time_limit
    assert psycopg.OperationalError in worker.ingest_file.autoretry_for and worker.ingest_file.max_retries == 3


def test_nightly_sync_is_scheduled():
    job = worker.app.conf.beat_schedule["nightly-folder-sync"]
    assert job["task"] == "indexing.worker.sync_folder"
    assert job["schedule"].hour == {2} and job["schedule"].minute == {15}


def test_duplicate_of_unindexed_document_gets_indexed(eager, samples):
    """A document processed but never indexed (e.g. migration missing) is not a 'duplicate' for good."""
    f = str(samples / "good" / "it_policy.txt")
    worker.ingest_file.delay(f).get()
    with connect() as conn:                       # pretend the first indexing never happened
        conn.execute("UPDATE chunks SET is_active = false")
        conn.execute("UPDATE documents SET indexed_at = NULL, chunk_count = NULL, is_active = false")
    again = worker.ingest_file.delay(f).get()
    assert again["status"] == "done" and again["indexed"] and again["chunks"] > 0
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM chunks WHERE is_active").fetchone()[0] > 0
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
