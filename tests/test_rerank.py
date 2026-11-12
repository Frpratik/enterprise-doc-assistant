"""Phase 3 Stage 2 tests: the cross-encoder reranker and search(rerank=True)."""
import numpy as np
import pytest

from indexing import rerank as rr
from indexing import sync
from indexing.embeddings import Embedder
from indexing.search import Hit, search
from ingestion.config import settings

SLUG = "models--" + settings.rerank_model.replace("/", "--")
needs_reranker = pytest.mark.skipif(not (settings.models_dir / SLUG).exists(), reason="reranker not downloaded")


def hit(chunk_id, content, heading="Doc > Section", vector_score=0.5):
    return Hit(0.0, chunk_id, "d", 1, "f.md", heading, {}, "text", content, vector_score=vector_score)


class FakeEmbedder(Embedder):
    name, dims = "fake-model", 26

    def embed_documents(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


# ---------- no model needed ----------
def test_reranker_reads_heading_path_and_text():
    assert rr.rerank_text(hit("a", "12 days", "Leave Policy > Sick Leave")) == "Leave Policy > Sick Leave\n12 days"
    assert rr.rerank_text(hit("a", "12 days", "")) == "12 days"


def test_rerank_orders_by_score_keeps_k_and_sets_fields(monkeypatch):
    monkeypatch.setattr(rr, "score_pairs", lambda q, texts: [1.0 if "sick" in t else -5.0 for t in texts])
    hits = [hit("a", "lunch"), hit("b", "sick leave is 12"), hit("c", "travel")]
    out = rr.rerank("sick days?", hits, k=2)
    assert [h.chunk_id for h in out][0] == "b" and len(out) == 2
    assert out[0].rerank_score == 1.0 and out[0].score == 1.0
    assert rr.rerank("q", [], k=3) == []


def test_missing_model_gives_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(rr, "_model", None)
    monkeypatch.setattr(settings, "models_dir", tmp_path)
    with pytest.raises(rr.RerankerNotInstalled, match="download_model"):
        rr.score_pairs("q", ["text"])


DOCS = {f"doc{i}.md": f"# Doc {i}\n\n## Topic {i}\nText about subject number {i} and some filler words.\n" for i in range(6)}
DOCS["leave.md"] = "# Leave\n\n## Sick Leave\nEmployees get 12 sick leaves per year.\n"


@pytest.fixture
def indexed(db_url, temp_data):
    src = temp_data / "source"
    src.mkdir()
    for name, text in DOCS.items():
        (src / name).write_text(text)
    fake = FakeEmbedder()
    sync.run_sync(src, embedder=fake)
    return fake


def test_search_rerank_fetches_more_then_keeps_k(indexed, monkeypatch):
    seen = {}

    def fake_scores(query, texts):
        seen["n"] = len(texts)
        return [10.0 if "sick leaves" in t else float(-i) for i, t in enumerate(texts)]

    monkeypatch.setattr(rr, "score_pairs", fake_scores)
    monkeypatch.setattr(settings, "rerank_candidates", 5)
    hits = search("how many sick leaves per year", k=2, embedder=indexed, rerank=True)
    assert seen["n"] == 5                                         # the reranker read 5 candidates ...
    assert len(hits) == 2 and hits[0].file_name == "leave.md"      # ... and the best 2 came back, best first
    assert hits[0].rerank_score == 10.0 and hits[0].score == 10.0
    assert hits[0].vector_score > 0                                # the original meaning score is kept


def test_search_without_rerank_is_unchanged(indexed):
    hits = search("sick leaves", k=3, embedder=indexed)
    assert all(h.rerank_score is None for h in hits)


# ---------- with the real model ----------
@needs_reranker
def test_real_reranker_prefers_the_answer():
    good, bad = rr.score_pairs("how many sick leaves do I get",
                               ["Employees get 12 sick leaves per year.", "Free lunch on Fridays."])
    assert good > bad + 5


@needs_reranker
def test_real_reranker_fixes_a_wrong_first_place():
    """Both chunks share words with the question; only one answers it."""
    q = "how long is the notice period when I resign"
    texts = ["Notice Period\nThe notice period for all permanent employees is 60 days.",
             "Period Tracking\nPeriod of probation notice: managers must notify HR when a long period of leave begins."]
    scores = rr.score_pairs(q, texts)
    assert scores[0] > scores[1]
