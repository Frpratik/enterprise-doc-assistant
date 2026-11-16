"""Phase 4 Stage 5 tests: the regression gate - record / replay of LLM replies, the verdict rules, the command, and the real gate on the real baseline."""
import json

import pytest

from indexing import answer as answer_mod
from indexing import eval_gate
from indexing.answer_golden import Golden, Question, load_golden
from indexing.answer_score import score_all
from indexing.confidence import Decision
from indexing.eval_gate import (GATE_BASELINE, Gate, LiveFallbackLLM, RecordingLLM, ReplayLLM, ReplayMiss, evaluate, format_gate, load_baseline,
                                prompt_key, question_of)
from indexing.llm import LLMResult, LLMUnavailable
from indexing.retrieve import Retrieval
from indexing.search import Hit
from ingestion.config import settings


# ---------- a tiny exam and baseline ----------
def q(i, answerable=True, **kw):
    base = dict(id=i, q=f"question {i}?", type="single_fact" if answerable else "near_topic", answerable=answerable, origin="v2", difficulty="easy",
                reference="r", facts=["15 days"], sources=[[["travel.docx", "within 15 days"]]]) if answerable else \
        dict(id=i, q=f"question {i}?", type="near_topic", answerable=False, origin="v2", reference="NOT_IN_DOCUMENTS", forbidden_terms=["x"])
    base.update(kw)
    return Question(**base)


GOLDEN = Golden(3, [], [q("A1"), q("A2"), q("A3"), q("U1", False), q("U2", False)])
BASELINE = {"meta": {"golden_version": 3, "model": "m", "prompt_version": "answer-v1"},
            "stable_correct": ["A1", "A2"], "known_failures": ["A3"], "evidence_in_context": ["A1", "A2"],
            "answerable_gate_passes": ["A1", "A2", "A3"], "gate_blocked_unanswerable": ["U1"], "unanswerable_refused": ["U1", "U2"], "replies": {}}
GOOD_CTX = [{"file": "travel.docx", "heading": "H", "chunk_id": "c", "content": "Claims are due within 15 days of returning."}]


def run(overrides=None, ids=("A1", "A2", "A3", "U1", "U2")):
    """A passing run, with per-question overrides: {'A1': {'outcome': ..., 'ctx': [...], 'gate': False}}."""
    scored, records = [], []
    for qid in ids:
        o = (overrides or {}).get(qid, {})
        answerable = qid.startswith("A")
        outcome = o.get("outcome", ("correct" if qid != "A3" else "incorrect") if answerable else "refused_ok")
        scored.append({"id": qid, "answerable": answerable, "outcome": outcome, "cause": o.get("cause", "generation"), "type": "x", "difficulty": "easy", "origin": "v2",
                       "status": "answered", "answered": True, "hedged": False, "latency_ms": None, **({"facts_missing": o["missing"]} if "missing" in o else {})})
        gate_ok = o.get("gate", qid != "U1")                       # U1 is blocked by the score gate in the baseline
        records.append({"id": qid, "status": "answered", "text": "x", "context": o.get("ctx", GOOD_CTX), "citations": [],
                        "gate": {"answerable": gate_ok, "top_rerank": 1.0, "top_vector": 0.7}})
    return scored, records


def check(overrides=None, missed=(), baseline=BASELINE, golden=GOLDEN, ids=("A1", "A2", "A3", "U1", "U2")):
    scored, records = run(overrides, ids)
    return evaluate(baseline, golden, scored, records, set(missed))


# ---------- the verdict ----------
def test_an_unchanged_system_passes():
    g = check()
    assert g.verdict == "PASS" and g.exit_code == 0 and not g.failures and not g.unverified and g.improvements == []


def test_a_new_failure_among_the_always_right_questions_fails_the_gate():
    g = check({"A1": {"outcome": "incorrect", "cause": "generation", "missing": ["15 days"]}})
    assert g.verdict == "FAIL" and g.exit_code == 1
    assert [(f["check"], f["id"]) for f in g.failures] == [("new_failure", "A1")] and "15 days" in g.failures[0]["detail"]


def test_a_question_that_was_always_wrong_may_stay_wrong_or_improve():
    assert check({"A3": {"outcome": "incorrect"}}).verdict == "PASS"
    better = check({"A3": {"outcome": "correct"}})
    assert better.verdict == "PASS" and better.improvements == ["A3"]


def test_lost_evidence_fails_even_when_the_answer_is_still_right():
    """A retrieval regression is visible without any LLM: the evidence is no longer given to the LLM."""
    g = check({"A2": {"ctx": [{"file": "other.md", "heading": "H", "chunk_id": "c", "content": "unrelated"}]}})
    assert g.verdict == "FAIL" and [(f["check"], f["id"]) for f in g.failures] == [("lost_evidence", "A2")]


def test_the_score_gate_must_not_start_blocking_answerable_questions():
    g = check({"A1": {"gate": False}})
    assert g.verdict == "FAIL" and ("gate_blocks_answerable", "A1") in [(f["check"], f["id"]) for f in g.failures]


def test_the_score_gate_must_keep_blocking_what_it_blocked():
    g = check({"U1": {"gate": True}})
    assert g.verdict == "FAIL" and [(f["check"], f["id"]) for f in g.failures] == [("gate_no_longer_blocks", "U1")]


@pytest.mark.parametrize("outcome", ["false_answer", "soft_refusal"])
def test_an_answer_to_an_unanswerable_question_fails(outcome):
    g = check({"U2": {"outcome": outcome}})
    assert g.verdict == "FAIL" and [(f["check"], f["id"]) for f in g.failures] == [("false_answer", "U2")]


def test_a_changed_prompt_cannot_be_verified_so_the_verdict_is_incomplete_not_pass():
    g = check(missed={"question A1?"})
    assert g.verdict == "INCOMPLETE" and g.exit_code == 2 and [u["id"] for u in g.unverified] == ["A1"] and not g.failures


def test_a_failure_elsewhere_beats_an_unverified_question():
    g = check({"A2": {"outcome": "incorrect"}}, missed={"question A1?"})
    assert g.verdict == "FAIL" and len(g.unverified) == 1


def test_an_llm_that_did_not_answer_is_neither_pass_nor_fail():
    g = check({"A1": {"outcome": "unavailable", "cause": "llm"}, "U2": {"outcome": "unavailable", "cause": "llm"}})
    assert g.verdict == "INCOMPLETE" and {u["id"] for u in g.unverified} == {"A1", "U2"} and not g.failures


def test_a_baseline_from_another_answer_key_is_refused():
    g = check(baseline={**BASELINE, "meta": {**BASELINE["meta"], "golden_version": 2}})
    assert g.verdict == "FAIL" and g.failures[0]["check"] == "answer_key_changed" and "re-record" in g.failures[0]["detail"]


def test_questions_that_were_not_run_are_ignored():
    assert check(ids=("A1", "U2")).verdict == "PASS"


def test_the_report_says_what_to_do():
    assert "FAIL" in format_gate(check({"A1": {"outcome": "incorrect"}})) and "[new_failure] A1" in format_gate(check({"A1": {"outcome": "incorrect"}}))
    inc = format_gate(check(missed={"question A1?"}))
    assert "INCOMPLETE" in inc and "--live" in inc and "re-record" in inc and "A1" in inc
    assert "PASS: no question that was always right got worse" in format_gate(check())
    assert "IMPROVEMENTS" in format_gate(check({"A3": {"outcome": "correct"}}))


# ---------- record / replay ----------
MSGS = [{"role": "system", "content": "rules"}, {"role": "user", "content": "<excerpts>\n[1] a\n\n[2] b\n</excerpts>\n\nQuestion: How many days?"}]


def test_a_prompt_has_a_stable_identity_that_changes_with_its_content():
    assert prompt_key(MSGS) == prompt_key(json.loads(json.dumps(MSGS)))
    swapped = [MSGS[0], {"role": "user", "content": MSGS[1]["content"].replace("[1] a", "[1] b").replace("[2] b", "[2] a")}]
    assert prompt_key(swapped) != prompt_key(MSGS) and len(prompt_key(MSGS)) == 64          # the order of the excerpts matters
    assert question_of(MSGS) == "How many days?"


def test_replay_returns_the_stored_reply_and_remembers_what_it_could_not_find():
    llm = ReplayLLM({prompt_key(MSGS): {"text": "15 days [1]", "model": "qwen"}})
    res = llm.chat(MSGS)
    assert res.text == "15 days [1]" and res.model == "qwen"
    other = [MSGS[0], {"role": "user", "content": "Question: another question?"}]
    with pytest.raises(ReplayMiss):
        llm.chat(other)
    assert llm.missed_questions == {"another question?"} and issubclass(ReplayMiss, LLMUnavailable)


class FakeLive:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tier="main", **kw):
        self.calls += 1
        return LLMResult("fresh", "live", tier, [], {}, 5)


def test_live_mode_replays_what_it_can_and_asks_the_real_llm_only_for_changed_prompts():
    live = FakeLive()
    llm = LiveFallbackLLM({prompt_key(MSGS): {"text": "stored", "model": "qwen"}}, live)
    assert llm.chat(MSGS).text == "stored" and live.calls == 0
    assert llm.chat([MSGS[0], {"role": "user", "content": "Question: new one?"}]).text == "fresh"
    assert live.calls == 1 and llm.asked_live == {"new one?"} and llm.missed_questions == set()


def test_recording_stores_every_reply_by_prompt_key():
    class Inner(FakeLive):
        last_ms = 123
    rec = RecordingLLM(Inner())
    rec.chat(MSGS)
    assert rec.replies == {prompt_key(MSGS): {"text": "fresh", "model": "live"}} and rec.last_ms == 123


def test_replay_works_inside_the_real_answer_function(monkeypatch):
    hits = [Hit(1.0, "c1", "d", 1, "travel.docx", "Travel > Claims", {"pages": [1]}, "text", "Claims are due within 15 days.", vector_score=0.8, rerank_score=5.0)]
    monkeypatch.setattr(answer_mod, "retrieve", lambda question, k=None, embedder=None: Retrieval(question, hits, Decision(True, "ok", "t", 5.0, 0.8)))
    stored = {prompt_key(answer_mod.build_messages("By when?", hits)): {"text": "Within 15 days [1].", "model": "qwen"}}
    ok = answer_mod.answer("By when?", llm=ReplayLLM(stored))
    assert ok.status == "answered" and ok.text == "Within 15 days [1]." and ok.model == "qwen"
    changed = ReplayLLM(stored)
    assert answer_mod.answer("A different question?", llm=changed).status == "llm_unavailable" and changed.missed_questions == {"A different question?"}


# ---------- the committed baseline ----------
def test_the_committed_baseline_is_consistent_with_the_answer_key():
    baseline, golden = load_baseline(), load_golden()
    ids = {x.id for x in golden.questions}
    answerable = {x.id for x in golden.answerable}
    assert baseline["meta"]["golden_version"] == golden.version and baseline["meta"]["model"] == settings.llm_main_model
    assert set(baseline["stable_correct"]) <= answerable and set(baseline["known_failures"]) <= answerable
    assert baseline["known_failures"] == ["A018", "A085"] and len(baseline["stable_correct"]) == 91
    assert set(baseline["stable_correct"]) <= set(baseline["evidence_in_context"]) <= set(baseline["answerable_gate_passes"])
    assert set(baseline["gate_blocked_unanswerable"]) <= {x.id for x in golden.unanswerable} <= ids
    assert len(baseline["gate_blocked_unanswerable"]) == 20 and len(baseline["unanswerable_refused"]) == 51
    assert len(baseline["replies"]) == baseline["meta"]["replies"] >= 100
    assert GATE_BASELINE.stat().st_size < 120_000                               # small enough to keep in git


def test_every_stored_reply_has_text_and_a_model():
    for key, reply in load_baseline()["replies"].items():
        assert len(key) == 64 and reply["text"].strip() and reply["model"] == "qwen/qwen3.8-27b"


# ---------- the command ----------
@pytest.fixture
def database_up(monkeypatch):
    """The command tests are about exit codes, not about Docker."""
    monkeypatch.setattr("indexing.db.can_connect", lambda *a, **k: True)


def test_the_command_exit_codes(monkeypatch, capsys, tmp_path, database_up):
    monkeypatch.setattr(eval_gate, "GATE_BASELINE", tmp_path / "missing.json")
    assert eval_gate.main([]) == 2 and "does not exist" in capsys.readouterr().out
    monkeypatch.setattr(eval_gate, "GATE_BASELINE", GATE_BASELINE)
    for verdict, code in (("PASS", 0), ("FAIL", 1), ("INCOMPLETE", 2)):
        monkeypatch.setattr(eval_gate, "run_gate", lambda *a, _v=verdict, **k: Gate(verdict=_v, info={"questions": 0, "answerable_correct": 0, "answerable": 0, "baseline_stable_correct": 0,
                                                                                                          "baseline_known_failures": [], "unanswerable_refused": 0}))
        assert eval_gate.main([]) == code
        assert verdict in capsys.readouterr().out


def test_a_broken_gate_is_never_a_pass(monkeypatch, capsys, database_up):
    def boom(*a, **k):
        raise RuntimeError("something broke")
    monkeypatch.setattr(eval_gate, "run_gate", boom)
    assert eval_gate.main([]) == 2 and "GATE ERROR: RuntimeError: something broke" in capsys.readouterr().out


def test_recording_needs_runs_and_reports_problems(monkeypatch, capsys, database_up):
    with pytest.raises(SystemExit):
        eval_gate.main(["--record"])
    monkeypatch.setattr(eval_gate, "record_baseline", lambda *a, **k: (_ for _ in ()).throw(ValueError("the recording pass does not reproduce the saved runs")))
    assert eval_gate.main(["--record", "--from", "x.json"]) == 2 and "NOT recorded" in capsys.readouterr().out


def test_a_missing_database_is_reported_and_is_never_a_pass(monkeypatch, capsys):
    monkeypatch.setattr("indexing.db.can_connect", lambda *a, **k: False)
    assert eval_gate.main([]) == 2 and "Database not reachable" in capsys.readouterr().out


# ---------- the real gate, on the real pipeline (needs Docker and the local models, no API key) ----------
needs_models = pytest.mark.skipif(
    not (settings.models_dir / ("models--" + settings.rerank_model.replace("/", "--"))).exists()
    or not any(settings.models_dir.rglob("tokenizer.json")), reason="local models not downloaded")


@needs_models
def test_the_answer_pipeline_has_not_regressed(samples, pg_server_url, monkeypatch):
    """THE regression gate: everything except the LLM is re-run, the stored LLM replies are replayed, and nothing that was always right may get worse."""
    from indexing.answer_golden import corpus_paths
    corpus_paths(load_golden().documents)                                     # fails with a clear message if the sample documents are missing
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    gate = eval_gate.run_gate(load_baseline(), load_golden())
    assert gate.verdict == "PASS", format_gate(gate)
    assert gate.info["answerable_correct"] == 91 and gate.info["unanswerable_refused"] == 51


@needs_models
def test_the_gate_fails_when_the_score_gate_is_broken(samples, pg_server_url, monkeypatch):
    """A known-bad change: the meaning threshold is so high that every answerable question is blocked."""
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    monkeypatch.setattr(settings, "min_vector_score", 0.99)
    golden = load_golden()
    ids = [x.id for x in golden.answerable][:5] + [x.id for x in golden.unanswerable][:2]
    gate = eval_gate.run_gate(load_baseline(), golden, ids=ids)
    assert gate.verdict == "FAIL" and "gate_blocks_answerable" in {f["check"] for f in gate.failures} and "new_failure" in {f["check"] for f in gate.failures}


@needs_models
def test_the_gate_does_not_pass_silently_when_what_the_llm_sees_changes(samples, pg_server_url, monkeypatch):
    """A known-bad change: the LLM gets only 2 excerpts. The stored replies no longer match the prompts, and the gate must say so."""
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    monkeypatch.setattr(settings, "answer_context_chunks", 2)
    golden = load_golden()
    gate = eval_gate.run_gate(load_baseline(), golden, ids=[x.id for x in golden.answerable][:8])
    assert gate.verdict != "PASS" and gate.unverified and all("prompt changed" in u["why"] for u in gate.unverified)
