"""Phase 2 Stage 6 tests: the retrieval golden set, the chunking baselines, and the retrieval gate."""
import json

import pytest

from indexing.retrieval_eval import (GOLDEN, SAMPLES, chunks_fixed, chunks_recursive, is_relevant, load_document,
                                     norm, run)
from ingestion.config import settings
from ingestion.models import Document, Element, ElementType, Location

golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
needs_model = pytest.mark.skipif(not any(settings.models_dir.rglob("tokenizer.json")), reason="model not downloaded")


@pytest.fixture(scope="module")
def docs(samples):
    return {name: load_document(SAMPLES / name) for name in golden["documents"]}


def test_every_answer_really_exists_in_its_document(docs):
    """Protects the golden set itself: a typo in an answer phrase would silently fake a result."""
    for item in golden["questions"]:
        for file_name, phrase in item["answers"]:
            assert norm(phrase) in norm(docs[file_name].full_text()), f"{item['q']} -> '{phrase}' not in {file_name}"


def test_questions_are_unique_and_documents_exist(samples):
    qs = [i["q"] for i in golden["questions"]]
    assert len(qs) == len(set(qs)) and len(qs) >= 25
    assert all((SAMPLES / n).exists() for n in golden["documents"])


def test_relevance_needs_right_file_and_phrase():
    answers = [["leave_policy.pdf", "12 sick leaves"]]
    assert is_relevant("leave_policy.pdf", "Employees get 12 sick leaves per year.", answers)
    assert not is_relevant("other.pdf", "Employees get 12 sick leaves per year.", answers)
    assert not is_relevant("leave_policy.pdf", "Employees get 8 casual leaves.", answers)


def _doc(text):
    d = Document(doc_id="t", file_name="t.txt", file_type="txt")
    d.add(Element(type=ElementType.PARAGRAPH, text=text, location=Location(page=1)))
    return d


def test_fixed_chunks_cover_text_exactly():
    doc = _doc("word " * 400)
    pieces = [c for c, _ in chunks_fixed(doc)]
    assert "".join(pieces) == doc.full_text()                         # nothing lost, nothing repeated
    assert all(len(p) <= 500 for p in pieces)


def test_recursive_chunks_respect_size_and_overlap():
    text = "\n\n".join(f"Paragraph {i}. " + "Some policy text here. " * 8 for i in range(20))
    pieces = [c for c, _ in chunks_recursive(_doc(text), size=500, overlap=100)]
    assert all(len(p) <= 500 + 101 for p in pieces)
    for i in range(1, len(pieces)):                                   # each piece starts with the end of the previous one
        assert pieces[i][:80] in pieces[i - 1]
    assert "Paragraph 19" in pieces[-1]


@needs_model
def test_retrieval_gate():
    """The retrieval eval gate: our chunking must keep finding the answers."""
    result = run(["structure"])["methods"]["structure"]
    assert result["recall@5"] >= 0.95, result["recall@5"]
    assert result["recall@1"] >= 0.90, result["recall@1"]
    assert result["mrr"] >= 0.90, result["mrr"]
