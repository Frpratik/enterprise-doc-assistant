"""Phase 4 Stage 2 tests: the runner (real retrieval + gate, scripted LLM). Needs the local models and Docker, no API key."""
import json

import pytest

from indexing import answer_run
from indexing.answer_golden import load_golden
from indexing.answer_score import score_all, summarize
from indexing.llm import LLMAuthError, LLMResult
from ingestion.config import settings

needs_models = pytest.mark.skipif(
    not (settings.models_dir / ("models--" + settings.rerank_model.replace("/", "--"))).exists()
    or not any(settings.models_dir.rglob("tokenizer.json")), reason="local models not downloaded")
pytestmark = needs_models


class ScriptedLLM:
    """Quotes the first sentence of excerpt [1] and cites it; can be told to fail on a given question."""

    def __init__(self, fail_on=None, auth_fail=False):
        self.calls, self.fail_on, self.auth_fail = [], fail_on, auth_fail

    def chat(self, messages, tier="main", **kw):
        question = messages[1]["content"].rsplit("Question: ", 1)[1]
        self.calls.append(question)
        if self.auth_fail:
            raise LLMAuthError("no key")
        if self.fail_on and self.fail_on in question:
            raise ValueError("boom")
        body = messages[1]["content"].split("<excerpts>\n", 1)[1]
        first_line = body.split("\n", 2)[1].strip()
        return LLMResult(f"{first_line[:90]} [1]", "scripted", tier, [], {}, 5)


@pytest.fixture(scope="module")
def golden(samples):
    import subprocess
    import sys
    from ingestion.config import PROJECT_ROOT
    if not (PROJECT_ROOT / "data" / "samples" / "eval2").exists():
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "make_eval_corpus.py")], check=True)
    return load_golden()


@pytest.fixture(scope="module")
def run(golden, pg_server_url):
    """One run on 7 questions (the 22 documents are indexed once for this whole file)."""
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "database_url", pg_server_url)
    answerable = [q for q in golden.answerable if q.origin == "v2"]
    ids = [answerable[0].id, answerable[1].id, next(q.id for q in golden.answerable if q.type == "multi_chunk"),
           next(q.id for q in golden.answerable if q.type == "table_lookup"),
           next(q.id for q in golden.unanswerable if q.type == "off_topic"), next(q.id for q in golden.unanswerable if q.type == "near_topic")]
    llm = ScriptedLLM()
    saved = (settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model)
    records = answer_run.generate(golden, model="some/model", llm=llm, ids=ids)
    restored = (settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model) == saved
    by_id = {r["id"]: r for r in records}
    yield {"records": records, "ids": [q.id for q in golden.questions if q.id in ids], "llm": llm, "restored": restored,
           "by_id": by_id, "off_topic_id": ids[4]}
    mp.undo()


def test_one_record_per_question_with_everything_needed_to_judge_it(run):
    assert [r["id"] for r in run["records"]] == run["ids"]
    for r in run["records"]:
        assert r["status"] in {"answered", "refused_gate", "refused_llm", "uncited"} and r["gate"]["top_vector"] is not None
        assert r["context"] and all({"file", "heading", "chunk_id", "content"} <= set(c) for c in r["context"])
        chunk_ids = {c["chunk_id"] for c in r["context"]}
        assert all(c["chunk_id"] in chunk_ids and 1 <= c["n"] <= len(r["context"]) for c in r["citations"])
    json.dumps(run["records"])                                         # everything is plain JSON: it can be saved and re-scored


def test_the_gate_still_blocks_off_topic_questions_before_the_llm(run):
    off = run["by_id"][run["off_topic_id"]]
    assert off["status"] == "refused_gate" and off["citations"] == [] and off["context"]
    assert len(run["llm"].calls) == len(run["records"]) - sum(r["status"] == "refused_gate" for r in run["records"])


def test_the_model_setting_is_restored_after_a_run(run):
    assert run["restored"]


def test_the_records_can_be_scored_straight_away(run, golden):
    scored = score_all(golden, run["records"])
    assert [s["id"] for s in scored] == run["ids"]
    summary = summarize(scored)
    assert summary["n"] == 6 and summary["n_answerable"] == 4 and summary["n_unanswerable"] == 2
    assert summary["confusion"]["should_refuse"]["refused"] >= 1


def test_saved_runs_can_be_re_scored_without_any_llm(run, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(answer_run, "RUNS", tmp_path)
    meta = answer_run.run_meta(load_golden(), "some/model", run["records"])
    path = answer_run.save_run(meta, run["records"], label="Some Model!")
    assert path.parent == tmp_path and path.name.startswith("answers_some_model_") and (tmp_path / "latest.json").exists()
    assert answer_run.main(["--rescore", str(path)]) == 0
    out = capsys.readouterr().out
    assert "no LLM calls" in out and "ANSWERABLE QUESTIONS (rule-based)" in out and "CONFUSION" in out


def test_one_crashing_question_does_not_lose_the_run(golden, pg_server_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    ids = [q.id for q in golden.answerable[:2]]
    victim = golden.answerable[0].q
    records = answer_run.generate(golden, llm=ScriptedLLM(fail_on=victim[:25]), ids=ids)
    assert records[0]["status"] == "error" and "boom" in records[0]["error"]
    assert records[1]["status"] in {"answered", "refused_gate", "refused_llm", "uncited"}
    assert score_all(golden, records)[0]["outcome"] == "unavailable"           # a crash is not counted as a wrong answer


def test_a_bad_api_key_stops_the_whole_run(golden, pg_server_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    with pytest.raises(LLMAuthError):
        answer_run.generate(golden, llm=ScriptedLLM(auth_fail=True), ids=[golden.answerable[0].id])
