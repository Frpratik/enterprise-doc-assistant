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


# ---------- Phase 4 Stage 4: repeats and fewer excerpts ----------
class SaltSpy(ScriptedLLM):
    """Remembers the cache salt that was set when the LLM was asked."""

    def __init__(self):
        super().__init__()
        self.salts = []

    def chat(self, messages, tier="main", **kw):
        self.salts.append(getattr(self, "salt", None))
        return super().chat(messages, tier=tier, **kw)


def test_repeats_ask_the_llm_again_each_time_with_their_own_cache_salt(golden, pg_server_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    ids = [q.id for q in golden.answerable if q.origin == "v2"][:3]
    llm = SaltSpy()
    runs = answer_run.generate_repeats(golden, llm=llm, repeats=3, ids=ids, first_salt=5)
    assert len(runs) == 3 and all([r["id"] for r in run] == ids for run in runs)
    assert sorted(set(llm.salts)) == [5, 6, 7]                                   # a different salt per repeat: the LLM is really asked again
    assert llm.salts.count(5) == llm.salts.count(6) == llm.salts.count(7) > 0    # and every repeat asked the same questions


def test_fewer_excerpts_for_the_llm_can_be_requested(golden, pg_server_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    qid = next(q.id for q in golden.answerable if q.origin == "v2")
    default = answer_run.generate(golden, llm=ScriptedLLM(), ids=[qid])[0]
    fewer = answer_run.generate(golden, llm=ScriptedLLM(), ids=[qid], k=2)[0]
    assert len(default["context"]) == settings.answer_context_chunks and len(fewer["context"]) == 2


def test_the_run_meta_records_the_salt_and_the_number_of_excerpts(golden):
    meta = answer_run.run_meta(golden, "some/model", [], salt=2, k=3)
    assert meta["cache_salt"] == 2 and meta["context_chunks"] == 3
    default = answer_run.run_meta(golden, "some/model", [])
    assert default["cache_salt"] == 0 and default["context_chunks"] == settings.answer_context_chunks


def test_the_repeat_command_saves_every_run_and_prints_the_spread(golden, pg_server_url, monkeypatch, tmp_path, capsys):
    from ingestion.config import PROJECT_ROOT
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    monkeypatch.setattr(answer_run, "RUNS", tmp_path)
    records = json.loads((PROJECT_ROOT / "evals" / "answers_v2_baseline.json").read_text(encoding="utf-8"))["records"]
    monkeypatch.setattr(answer_run, "generate_repeats", lambda *a, **k: [records, records, records])
    assert answer_run.main(["--repeat", "3", "--label", "demo"]) == 0
    out = capsys.readouterr().out
    names = sorted(p.name for p in tmp_path.glob("answers_demo_r*.json"))
    assert len(names) == 3 and [n.split("_")[2] for n in names] == ["r1", "r2", "r3"]
    assert "run 1: correct 91/93" in out and "STABILITY over 3 runs" in out and "FLAKY questions (right in some runs, wrong in others): 0" in out


# ---------- a stalled LLM (rate limit / daily quota) stops the run instead of producing a fake result ----------
class FlakyLLM(ScriptedLLM):
    """fails (as if every model were unavailable) when the pattern says True, otherwise answers."""

    def __init__(self, pattern):
        super().__init__()
        self.pattern, self.n = pattern, 0

    def chat(self, messages, tier="main", **kw):
        from indexing.llm import LLMUnavailable
        fail = self.pattern[self.n % len(self.pattern)]
        self.n += 1
        if fail:
            raise LLMUnavailable([])
        return super().chat(messages, tier=tier, **kw)


def test_the_run_stops_when_the_llm_stalls_and_says_why(golden, pg_server_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    llm = FlakyLLM([True])
    ids = [q.id for q in golden.answerable][:10]
    with pytest.raises(answer_run.LLMStalled, match="cached"):
        answer_run.generate(golden, llm=llm, ids=ids)
    assert llm.n == answer_run.STALL_AFTER                                       # it stopped at once, it did not grind through all 10


def test_a_few_failures_in_between_successes_do_not_stop_the_run(golden, pg_server_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    llm = FlakyLLM([True, True, True, True, False])                                # 4 failures, then a success, again and again
    ids = [q.id for q in golden.answerable][:10]
    records = answer_run.generate(golden, llm=llm, ids=ids)
    assert len(records) == 10 and "llm_unavailable" in {r["status"] for r in records} and "answered" in {r["status"] for r in records}


def test_the_command_exits_with_2_and_refuses_to_save_a_baseline_from_an_incomplete_run(golden, pg_server_url, monkeypatch, tmp_path, capsys):
    from ingestion.config import PROJECT_ROOT
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    monkeypatch.setattr(answer_run, "RUNS", tmp_path)
    monkeypatch.setattr(answer_run, "BASELINE", tmp_path / "baseline.json")
    records = json.loads((PROJECT_ROOT / "evals" / "answers_v2_baseline.json").read_text(encoding="utf-8"))["records"]
    gappy = [dict(r, status="llm_unavailable", text="busy", citations=[]) if r["id"] in ("A001", "A002") else r for r in records]
    monkeypatch.setattr(answer_run, "generate_repeats", lambda *a, **k: [gappy])
    assert answer_run.main(["--save-baseline"]) == 2
    out = capsys.readouterr().out
    assert "WARNING: the LLM never answered 2 questions" in out and "NOT saving a baseline" in out and not (tmp_path / "baseline.json").exists()


def test_the_command_reports_a_stall_clearly(golden, pg_server_url, monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", pg_server_url)

    def stall(*a, **k):
        raise answer_run.LLMStalled("the LLM did not answer 5 questions in a row")
    monkeypatch.setattr(answer_run, "generate_repeats", stall)
    assert answer_run.main([]) == 2 and "STOPPED: the LLM did not answer 5 questions in a row" in capsys.readouterr().out


def test_an_incomplete_repeat_run_exits_with_2(golden, pg_server_url, monkeypatch, tmp_path, capsys):
    from ingestion.config import PROJECT_ROOT
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    monkeypatch.setattr(answer_run, "RUNS", tmp_path)
    records = json.loads((PROJECT_ROOT / "evals" / "answers_v2_baseline.json").read_text(encoding="utf-8"))["records"]
    gappy = [dict(r, status="llm_unavailable", text="busy", citations=[]) if r["id"] == "A001" else r for r in records]
    monkeypatch.setattr(answer_run, "generate_repeats", lambda *a, **k: [records, records, gappy])
    assert answer_run.main(["--repeat", "3"]) == 2
    out = capsys.readouterr().out
    assert "NEVER ANSWERED BY THE LLM: 1" in out and "WARNING: the LLM never answered some questions: run 3: 1" in out


def test_the_stall_guard_can_be_switched_off_for_the_replay_gate(golden, pg_server_url, monkeypatch):
    """Replaying stored replies, a prompt that changed has no reply on purpose: that must not stop the run."""
    monkeypatch.setattr(settings, "database_url", pg_server_url)
    ids = [q.id for q in golden.answerable][:8]
    records = answer_run.generate_repeats(golden, llm=FlakyLLM([True]), ids=ids, stall_after=None)[0]
    assert len(records) == 8 and {r["status"] for r in records} <= {"llm_unavailable", "refused_gate"}
