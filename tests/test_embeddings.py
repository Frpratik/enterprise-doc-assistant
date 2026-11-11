"""Phase 2 Stage 2 tests: embeddings - meaning, determinism, batching, and the cache."""
import numpy as np
import pytest

from indexing.db import connect
from indexing.embeddings import Embedder, OpenAIEmbedder, embed_with_cache, get_embedder, text_hash
from ingestion.config import settings

needs_model = pytest.mark.skipif(not any(settings.models_dir.rglob("tokenizer.json")),
                                 reason="model not downloaded: python scripts/download_model.py")


@pytest.fixture(scope="module")
def model():
    return get_embedder("local")


def cos(a, b):
    return float(np.dot(a, b))          # vectors are normalized, so dot product = cosine similarity


@needs_model
def test_shape_and_normalized(model):
    v = model.embed_documents(["Employees get 12 sick leaves per year."])[0]
    assert model.dims == 384 and v.shape == (384,)
    assert abs(np.linalg.norm(v) - 1) < 1e-4


@needs_model
def test_same_text_same_vector(model):
    a, b = model.embed_documents(["Notice period is 60 days."] * 2)
    assert np.allclose(a, b)


@needs_model
def test_similar_meaning_is_closer(model):
    sick, medical, laptop = model.embed_documents([
        "How many sick leaves do I get?",
        "What is the number of medical leave days?",
        "Company laptops must be locked when unattended."])
    assert cos(sick, medical) > cos(sick, laptop) + 0.1


@needs_model
def test_question_finds_the_right_chunk(model):
    chunks = [
        "Leave Policy 2026 > 1. Sick Leave\n\nEmployees get 12 sick leaves per year.",
        "Leave Policy 2026 > 4. Notice Period\n\nThe notice period for all permanent employees is 60 days.",
        "IT Policy\n\nPasswords must be changed every 90 days.",
        "Remote Work Policy > Eligibility\n\nEmployees can work from home 2 days a week.",
    ]
    vecs = model.embed_documents(chunks)
    for question, expected in [("how many days of sick leave do I have", 0),
                               ("how long do I have to serve after resigning", 1),
                               ("how often should I change my password", 2),
                               ("can I work from home", 3)]:
        q = model.embed_query(question)
        assert int(np.argmax([cos(q, v) for v in vecs])) == expected, question


@needs_model
def test_query_instruction_is_added_only_to_questions(model):
    q = "notice period"
    assert not np.allclose(model.embed_query(q), model.embed_documents([q])[0])


@needs_model
def test_batching_keeps_order(model):
    texts = [f"Policy number {i} is about topic {i}." for i in range(150)]
    all_at_once = model.embed_documents(texts)
    one_by_one = [model.embed_documents([texts[i]])[0] for i in (0, 77, 149)]
    for got, i in zip(one_by_one, (0, 77, 149)):
        assert np.allclose(all_at_once[i], got, atol=1e-4)


# ---------------- cache (fake model, real database) ----------------
class CountingEmbedder(Embedder):
    """A fake model that counts how many texts it was asked to embed."""
    name, dims = "fake-model", 4

    def __init__(self):
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += len(texts)
        return [np.array([len(t), 1, 0, 0], dtype=np.float32) for t in texts]


def test_cache_embeds_each_text_only_once(db_url):
    fake = CountingEmbedder()
    with connect() as conn:
        vecs, stats = embed_with_cache(conn, ["a", "bb", "ccc"], fake)
    assert stats == {"cached": 0, "new": 3} and fake.calls == 3
    with connect() as conn:                                     # e.g. version 2 of the same document
        vecs2, stats2 = embed_with_cache(conn, ["a", "bb", "dddd"], fake)
    assert stats2 == {"cached": 2, "new": 1} and fake.calls == 4
    assert np.allclose(vecs2[0], vecs[0])


def test_duplicates_in_one_call_are_embedded_once(db_url):
    fake = CountingEmbedder()
    with connect() as conn:
        vecs, stats = embed_with_cache(conn, ["same", "other", "same"], fake)
    assert fake.calls == 2 and stats == {"cached": 0, "new": 2}
    assert np.allclose(vecs[0], vecs[2])


def test_different_models_are_cached_separately(db_url):
    a, b = CountingEmbedder(), CountingEmbedder()
    b.name = "another-model"
    with connect() as conn:
        embed_with_cache(conn, ["hello"], a)
        embed_with_cache(conn, ["hello"], b)
        rows = conn.execute("SELECT embed_model FROM embeddings WHERE embed_hash = %s",
                            (text_hash("hello"),)).fetchall()
    assert sorted(r[0] for r in rows) == ["another-model", "fake-model"]


def test_openai_embedder_batches_requests():
    class FakeOpenAI:
        def __init__(self):
            self.requests = []
            self.embeddings = self

        def create(self, model, input):
            self.requests.append(len(input))
            data = [type("D", (), {"index": i, "embedding": [1.0, float(i)]})() for i in range(len(input))]
            return type("R", (), {"data": data})()

    client = FakeOpenAI()
    vecs = OpenAIEmbedder(client=client).embed_documents([f"t{i}" for i in range(250)])
    assert client.requests == [100, 100, 50] and len(vecs) == 250
