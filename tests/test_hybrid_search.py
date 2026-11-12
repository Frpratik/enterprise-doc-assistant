"""Phase 3 Stage 1 tests: keyword search (IDF), RRF merging, hybrid search, and the hybrid gate."""
import json

import numpy as np
import pytest

from indexing import sync
from indexing.db import connect
from indexing.embeddings import Embedder
from indexing.retrieval_eval import SAMPLES, is_relevant, load_document, norm
from indexing.search import keyword_terms, rrf_merge, search
from indexing.search_eval import SETS
from ingestion.config import settings


class FakeEmbedder(Embedder):
    name, dims = "fake-model", 26

    def embed_documents(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


DOCS = {
    "handbook.md": "# Handbook\n\n## Reimbursement Limits\nInternet up to 1,000 per month.\n\n## IT Support\nHelpdesk: ext 4455\n",
    "leave.md": "# Leave\n\n## Sick Leave\nEmployees get 12 sick leaves per year.\n",
    "travel.md": "# Travel\n\n## Hotels\nEmployees may stay in hotels up to Rs 4,000 per night.\n",
    "benefits.md": "# Benefits\n\n## Insurance\nEmployees and their families get health insurance.\n",
    "contacts.md": "# Contacts\n\n## Mail\nWrite to payroll@acme-example.com for salary questions.\n",
}


@pytest.fixture
def indexed(db_url, temp_data):
    src = temp_data / "source"
    src.mkdir()
    for name, text in DOCS.items():
        (src / name).write_text(text)
    fake = FakeEmbedder()
    sync.run_sync(src, embedder=fake)
    return fake


# ---------- pure functions ----------
def test_keyword_terms_keep_emails_and_amounts_whole():
    assert keyword_terms("Mail it@acme-example.com about Rs 25,000?") == ["mail", "it@acme-example.com", "about", "rs", "25,000"]
    assert keyword_terms("sick sick SICK") == ["sick"]
    assert keyword_terms("  ?! ") == []


def test_rrf_chunk_found_by_both_lists_wins():
    scores = rrf_merge(["a", "b", "c"], ["c", "b"], k=60, keyword_weight=1.0)
    assert scores["b"] > scores["a"] and scores["c"] > scores["a"]            # both lists beat one list
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 62)


def test_rrf_keyword_weight_lowers_the_keyword_list():
    full = rrf_merge(["a"], ["b"], k=60, keyword_weight=1.0)
    low = rrf_merge(["a"], ["b"], k=60, keyword_weight=0.1)
    assert full["a"] == full["b"] and low["a"] > low["b"] and low["b"] == pytest.approx(0.1 / 61)
    assert rrf_merge([], []) == {}


# ---------- against a real database ----------
def test_keyword_search_finds_exact_terms(indexed):
    assert search("ext 4455", k=1, embedder=indexed, mode="keyword")[0].file_name == "handbook.md"
    assert search("payroll@acme-example.com", k=1, embedder=indexed, mode="keyword")[0].file_name == "contacts.md"


def test_rare_term_counts_more_than_common_term(indexed):
    """'employees' is in 3 chunks, '4455' in 1: the chunk with the rare term must win."""
    hits = search("employees 4455", k=5, embedder=indexed, mode="keyword")
    assert hits[0].file_name == "handbook.md"


def test_keyword_search_looks_at_headings_too(indexed):
    """'reimbursement' appears only in a heading, never in the body text."""
    hit = search("reimbursement", k=1, embedder=indexed, mode="keyword")[0]
    assert hit.file_name == "handbook.md" and "Reimbursement" in hit.heading_path


def test_keyword_search_stems_words(indexed):
    assert search("sick leave", k=1, embedder=indexed, mode="keyword")[0].file_name == "leave.md"
    assert search("hotel", k=1, embedder=indexed, mode="keyword")[0].file_name == "travel.md"


def test_hits_say_who_found_them_and_always_carry_a_vector_score(indexed):
    hits = search("ext 4455", k=10, embedder=indexed, mode="hybrid", candidates=3)
    kinds = {h.found_by for h in hits}
    assert "both" in kinds or "keyword" in kinds
    assert all(0.0 < h.vector_score <= 1.0 for h in hits)                       # needed for the "I don't know" rule
    only_keyword = [h for h in hits if h.found_by == "keyword"]
    assert all(h.keyword_score for h in only_keyword)


def test_hybrid_rescues_an_exact_term_that_vectors_miss(indexed, monkeypatch):
    """Fake vectors are just letter counts, so they cannot tell '4455' from anything: keywords must carry it."""
    monkeypatch.setattr(settings, "rrf_keyword_weight", 1.0)
    assert search("ext 4455", k=1, embedder=indexed, mode="hybrid")[0].file_name == "handbook.md"


def test_stop_word_only_question_does_not_crash(indexed):
    assert search("how do i", k=3, embedder=indexed, mode="keyword") == []
    assert len(search("how do i", k=3, embedder=indexed, mode="hybrid")) == 3      # vectors still answer


def test_deleted_document_is_not_found_by_keywords(indexed):
    with connect() as conn:
        doc_id = conn.execute("SELECT doc_id FROM documents WHERE file_name='handbook.md'").fetchone()[0]
    sync.delete_document(doc_id)
    assert search("ext 4455", k=5, embedder=indexed, mode="keyword") == []


def test_unknown_mode_is_rejected(indexed):
    with pytest.raises(ValueError):
        search("x", embedder=indexed, mode="magic")


# ---------- the evaluation sets themselves + the gate ----------
needs_model = pytest.mark.skipif(not any(settings.models_dir.rglob("tokenizer.json")), reason="model not downloaded")


def test_exact_term_set_answers_really_exist(samples):
    g = json.loads(SETS["exact_terms"].read_text(encoding="utf-8"))
    docs = {n: load_document(SAMPLES / n) for n in g["documents"]}
    for item in g["questions"]:
        assert any(norm(phrase) in norm(docs[f].full_text()) for f, phrase in item["answers"]), item["q"]
    qs = [i["q"] for i in g["questions"]]
    assert len(qs) == len(set(qs))


@needs_model
def test_hybrid_gate(samples, pg_server_url, monkeypatch):
    """Hybrid search must stay good on both question sets and must not hurt either one."""
    from indexing import search_eval
    monkeypatch.setattr(settings, "database_url", pg_server_url)          # the eval makes its own temporary database
    result = search_eval.run(["vector", "hybrid"])
    para, exact = result["sets"]["paraphrase"], result["sets"]["exact_terms"]
    assert para["hybrid"]["mrr"] >= 0.95 and para["hybrid"]["recall@5"] >= 0.95
    assert exact["hybrid"]["mrr"] >= 0.90 and exact["hybrid"]["recall@5"] >= 0.95
    assert para["hybrid"]["mrr"] >= para["vector"]["mrr"] - 0.01       # hybrid must not hurt normal questions
    assert exact["hybrid"]["mrr"] >= exact["vector"]["mrr"]            # and must not hurt exact-term ones
