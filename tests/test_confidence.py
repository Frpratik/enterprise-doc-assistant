"""Phase 3 Stage 3 tests: the "I don't know" gate."""
import json

import pytest

from indexing.confidence import NO_ANSWER_MESSAGE, decide
from indexing.search import Hit
from ingestion.config import settings


def hit(rerank, vector):
    return Hit(0.0, "c", "d", 1, "f.md", "Doc > Section", {}, "text", "text", vector_score=vector, rerank_score=rerank)


# ---------- the decision rule (no models, no database) ----------
def test_no_results_means_no_answer():
    d = decide([])
    assert not d.answerable and d.code == "no_results"


def test_clear_match_is_answerable():
    d = decide([hit(6.4, 0.80), hit(-1.9, 0.66)])
    assert d.answerable and d.code == "ok" and d.top_rerank == 6.4 and d.top_vector == 0.80


def test_unrelated_question_is_blocked():
    d = decide([hit(-11.2, 0.37), hit(-11.4, 0.35)])
    assert not d.answerable and d.code == "weak_match"


def test_either_signal_alone_can_block():
    assert not decide([hit(-10.5, 0.80)]).answerable             # reranker says unrelated
    assert not decide([hit(3.0, 0.40)]).answerable               # meaning score says unrelated


def test_the_best_hit_counts_not_the_first_one():
    """A weak first hit must not block a question when a later hit is good."""
    assert decide([hit(-10.5, 0.45), hit(2.0, 0.70)]).answerable


def test_thresholds_are_exclusive_at_the_boundary(monkeypatch):
    monkeypatch.setattr(settings, "min_rerank_score", -10.0)
    monkeypatch.setattr(settings, "min_vector_score", 0.56)
    assert decide([hit(-10.0, 0.56)]).answerable                 # exactly on the line: still answered
    assert not decide([hit(-10.01, 0.70)]).answerable
    assert not decide([hit(0.0, 0.5599)]).answerable


def test_without_reranker_only_the_meaning_score_is_used():
    assert decide([hit(None, 0.70)]).answerable
    d = decide([hit(None, 0.40)])
    assert not d.answerable and "meaning" in d.reason


def test_the_reason_is_readable_and_the_user_message_is_fixed():
    assert "rerank" in decide([hit(-11.0, 0.70)]).reason
    assert "company documents" in NO_ANSWER_MESSAGE


# ---------- the question sets ----------
def test_unanswerable_set_is_well_formed():
    from indexing.abstain_eval import UNANSWERABLE, load_questions
    qs = load_questions()
    flat = qs["answerable"] + qs["off_topic"] + qs["near_topic"]
    assert len(flat) == len(set(flat))                           # no question appears twice, in any group
    assert len(qs["off_topic"]) >= 10 and len(qs["near_topic"]) >= 20 and len(qs["answerable"]) >= 40
    assert json.loads(UNANSWERABLE.read_text(encoding="utf-8"))["version"] == 1


# ---------- the gate with the real models ----------
needs_models = pytest.mark.skipif(
    not (settings.models_dir / ("models--" + settings.rerank_model.replace("/", "--"))).exists()
    or not any(settings.models_dir.rglob("tokenizer.json")), reason="models not downloaded")


@needs_models
def test_abstain_gate(samples, pg_server_url, monkeypatch):
    """The gate must never block an answerable question, and must block every off-topic one."""
    from indexing import abstain_eval
    monkeypatch.setattr(settings, "database_url", pg_server_url)       # the eval makes its own temporary database
    res = abstain_eval.run()
    s = res["summary"]
    assert s["answerable_answered"] == 1.0, [r["q"] for r in res["groups"]["answerable"] if not r["answered"]]
    assert s["off_topic_blocked"] == 1.0, [r["q"] for r in res["groups"]["off_topic"] if r["answered"]]
    assert s["near_topic_blocked"] >= 0.10                             # a floor, not a goal: the LLM step handles the rest
