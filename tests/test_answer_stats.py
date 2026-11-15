"""Phase 4 Stage 4 tests: spread, reliability, paired comparison and gate noise. Pure functions on hand-made runs: no LLM, no database."""
import json

import pytest

from indexing import answer_stats
from indexing.answer_golden import load_golden
from indexing.answer_stats import (accuracy, bootstrap_mean_accuracy, compare, correct_map, gate_false_alarms, mcnemar_exact,
                                   needed_discordant, new_failures, paired_bootstrap, reliability, stability_report, text_stability,
                                   unanswerable_stability)


def s(qid, ok, answerable=True, outcome=None):
    return {"id": qid, "answerable": answerable, "outcome": outcome or ("correct" if ok else "incorrect")}


def run(**ok):
    """run(q1=True, q2=False) -> a scored run with those answerable questions."""
    return [s(q, v) for q, v in ok.items()]


# ---------- one run ----------
def test_correct_map_and_accuracy_look_only_at_answerable_questions():
    scored = run(q1=True, q2=False, q3=True) + [s("U1", True, answerable=False, outcome="refused_ok")]
    assert correct_map(scored) == {"q1": True, "q2": False, "q3": True}
    assert accuracy(correct_map(scored)) == pytest.approx(2 / 3) and accuracy({}) == 0.0


# ---------- several runs of the same system ----------
THREE = [run(q1=True, q2=True, q3=True, q4=False, q5=True),
         run(q1=True, q2=True, q3=False, q4=False, q5=True),
         run(q1=True, q2=False, q3=False, q4=False, q5=True)]


def test_reliability_pass_hat_k_is_stricter_than_the_average():
    r = reliability(THREE)
    assert r["runs"] == 3 and r["questions"] == 5
    assert r["accuracy_per_run"] == [0.8, 0.6, 0.4]
    assert r["pass_at_1_mean"] == pytest.approx(0.6)                              # (4 + 3 + 2) / 15
    assert r["pass_hat_k"]["k"] == 2 and r["pass_at_k"]["k"] == 4                 # right in ALL 3 runs: q1, q5 | in at least one: all but q4
    assert r["stable_correct"] == ["q1", "q5"] and r["stable_wrong"] == ["q4"]
    assert r["flaky"] == {"q2": 2, "q3": 1}
    assert r["pass_hat_k"]["rate"] < r["pass_at_1_mean"] < r["pass_at_k"]["rate"]


def test_unanswerable_questions_and_missing_questions_do_not_disturb_reliability():
    a = run(q1=True, q2=True) + [s("U1", True, answerable=False, outcome="refused_ok")]
    b = run(q1=True) + [s("U1", False, answerable=False, outcome="false_answer")]          # q2 missing from this run
    r = reliability([a, b])
    assert r["questions"] == 1 and r["stable_correct"] == ["q1"]


def test_bootstrap_mean_accuracy():
    perfect = bootstrap_mean_accuracy([run(q1=True, q2=True, q3=True)] * 3)
    assert perfect == {"mean": 1.0, "ci": [1.0, 1.0], "questions": 3}
    mixed = bootstrap_mean_accuracy(THREE)
    assert mixed["mean"] == pytest.approx(0.6) and mixed["ci"][0] <= 0.6 <= mixed["ci"][1] and mixed["ci"][0] < mixed["ci"][1]
    assert bootstrap_mean_accuracy(THREE) == mixed                                  # the same seed gives the same interval
    assert bootstrap_mean_accuracy(THREE, seed=2)["mean"] == mixed["mean"]


def test_text_stability_ignores_whitespace_case_and_citation_markers():
    def rec(i, text, status="answered"):
        return {"id": i, "text": text, "status": status}
    runs = [[rec("a", "You get 12 days [1]."), rec("b", "Within 15 days [1]."), rec("c", "x", "refused_llm")],
            [rec("a", "you get  12 days [2]."), rec("b", "Within 30 days [1]."), rec("c", "x", "answered")]]
    t = text_stability(runs)
    # a: same text after ignoring case / spaces / [n]; b: different number; c: same text "x" but a different status
    assert t["questions"] == 3 and t["identical_text"]["k"] == 2 and t["same_status"]["k"] == 2


def test_the_dangerous_failure_is_found_even_if_it_happens_in_one_run_only():
    runs = [[s("U1", True, False, "refused_ok"), s("U2", True, False, "refused_ok"), s("U3", True, False, "refused_ok")],
            [s("U1", True, False, "refused_ok"), s("U2", False, False, "false_answer"), s("U3", False, False, "soft_refusal")],
            [s("U1", True, False, "refused_ok"), s("U2", True, False, "refused_ok"), s("U3", True, False, "refused_ok")]]
    u = unanswerable_stability(runs)
    assert u["answered_in_any_run"] == ["U2", "U3"] and u["flipped"] == ["U2", "U3"] and u["refused_every_time"]["k"] == 1


# ---------- comparing two systems on the same questions ----------
@pytest.mark.parametrize("only_a, only_b, expected", [
    (6, 0, 0.03125), (5, 0, 0.0625), (0, 0, 1.0), (3, 3, 1.0), (1, 0, 1.0), (10, 2, 0.0386), (2, 10, 0.0386), (0, 8, 0.0078)])
def test_mcnemar_exact_known_values(only_a, only_b, expected):
    assert mcnemar_exact(only_a, only_b) == pytest.approx(expected, abs=0.0001)


def test_how_many_changes_are_needed_to_be_significant():
    assert needed_discordant() == 6 and needed_discordant(0.01) == 8
    assert mcnemar_exact(6, 0) < 0.05 <= mcnemar_exact(5, 0)


def test_paired_bootstrap():
    same = {f"q{i}": i % 2 == 0 for i in range(20)}
    assert paired_bootstrap(same, dict(same)) == {"diff": 0.0, "ci": [0.0, 0.0]}
    all_right, all_wrong = {f"q{i}": True for i in range(20)}, {f"q{i}": False for i in range(20)}
    assert paired_bootstrap(all_right, all_wrong) == {"diff": 1.0, "ci": [1.0, 1.0]}
    a = {f"q{i}": i >= 3 for i in range(40)}
    b = {f"q{i}": True for i in range(40)}
    boot = paired_bootstrap(a, b)
    assert boot["diff"] == pytest.approx(-3 / 40) and boot["ci"][0] <= boot["diff"] <= boot["ci"][1]
    assert paired_bootstrap(a, b) == boot


def test_compare_two_runs():
    a = run(q1=True, q2=True, q3=False, q4=False, q5=True)
    b = run(q1=True, q2=False, q3=True, q4=False, q5=True)
    c = compare(a, b)
    assert c["only_a_correct"] == ["q2"] and c["only_b_correct"] == ["q3"] and c["both_wrong"] == ["q4"]
    assert c["accuracy_a"] == c["accuracy_b"] == 0.6 and c["p_value"] == 1.0 and c["diff"] == 0.0


def test_a_small_difference_on_a_small_exam_is_not_significant_a_big_one_is():
    ids = [f"q{i}" for i in range(93)]
    base = [s(i, True) for i in ids]
    worse_by_3 = [s(i, k >= 3) for k, i in enumerate(ids)]
    worse_by_6 = [s(i, k >= 6) for k, i in enumerate(ids)]
    assert compare(base, worse_by_3)["p_value"] == 0.25 and compare(base, worse_by_6)["p_value"] < 0.05


# ---------- a regression gate: no new failure among the questions that used to be always right ----------
def test_new_failures_only_counts_questions_that_were_always_right():
    baseline = [run(q1=True, q2=True, q3=True), run(q1=True, q2=True, q3=False)]
    new = run(q1=True, q2=False, q3=False)
    assert new_failures(baseline, new) == ["q2"]                                    # q3 was flaky in the baseline: not a regression


def test_gate_noise_is_measured_by_leaving_each_run_out():
    runs = [run(q1=True, q2=True), run(q1=True, q2=False), run(q1=True, q2=True)]
    g = gate_false_alarms(runs)
    assert g["false_alarms_per_run"] == [0, 1, 0] and g["runs_with_a_false_alarm"] == 1 and g["questions"] == ["q2"]
    steady = gate_false_alarms([run(q1=True, q2=False)] * 3)
    assert steady["false_alarms_per_run"] == [0, 0, 0] and steady["runs_with_a_false_alarm"] == 0       # a stable failure is not noise
    with pytest.raises(ValueError, match="at least 3 runs"):
        gate_false_alarms(runs[:2])


# ---------- the reports, from saved run files ----------
@pytest.fixture(scope="module")
def baseline_records():
    from ingestion.config import PROJECT_ROOT
    return json.loads((PROJECT_ROOT / "evals" / "answers_v2_baseline.json").read_text(encoding="utf-8"))


def save(tmp_path, name, meta, records):
    path = tmp_path / name
    path.write_text(json.dumps({"meta": meta, "records": records}), encoding="utf-8")
    return str(path)


def broken(records, qid):
    """The same records, with one answer changed to a wrong number."""
    out = json.loads(json.dumps(records))
    for r in out:
        if r["id"] == qid:
            r["text"] = "The answer is 999 [1]."
    return out


def test_the_stability_command_reports_flaky_questions(baseline_records, tmp_path, capsys):
    meta, records = baseline_records["meta"], baseline_records["records"]
    files = [save(tmp_path, "r1.json", meta, records), save(tmp_path, "r2.json", meta, broken(records, "A001")),
             save(tmp_path, "r3.json", meta, records)]
    assert answer_stats.main(["--stability", *files]) == 0
    out = capsys.readouterr().out
    assert "STABILITY over 3 runs" in out and "FLAKY questions (right in some runs, wrong in others): 1" in out and "A001: correct in 2 of 3 runs" in out
    assert "pass^3" in out and "NOISE OF A 'NO NEW FAILURE' GATE" in out and "[0, 1, 0]" in out
    assert "at least 6 questions must change" in out


def test_the_compare_command_says_when_a_difference_is_not_significant(baseline_records, tmp_path, capsys):
    meta, records = baseline_records["meta"], baseline_records["records"]
    a, b = save(tmp_path, "a.json", meta, records), save(tmp_path, "b.json", {**meta, "model": "other/model"}, broken(records, "A001"))
    assert answer_stats.main(["--compare", a, b]) == 0
    out = capsys.readouterr().out
    assert "only qwen/qwen3.8-27b right: 1" in out and "NOT statistically different" in out and "A001" in out


def test_the_stability_report_has_everything_the_log_quotes(baseline_records):
    golden = load_golden()
    from indexing.answer_score import score_all
    recs = baseline_records["records"]
    runs = [recs, recs, recs]
    rep = stability_report(golden, [score_all(golden, r) for r in runs], runs, ["m"] * 3)
    assert rep["reliability"]["pass_hat_k"]["rate"] == rep["reliability"]["pass_at_k"]["rate"] == pytest.approx(91 / 93, abs=1e-3)
    assert rep["reliability"]["flaky"] == {} and rep["text"]["identical_text"]["rate"] == 1.0
    assert rep["gate_noise"]["runs_with_a_false_alarm"] == 0 and rep["detectable"] == {"discordant_needed": 6, "n_answerable": 93}
    assert rep["unanswerable"]["refused_every_time"]["rate"] == 1.0


# ---------- questions the LLM never answered (a quota is not a changed mind) ----------
def gone(qid, answerable=True):
    return {"id": qid, "answerable": answerable, "outcome": "unavailable"}


def test_a_question_the_llm_never_answered_is_left_out_not_counted_as_a_flip():
    runs = [run(q1=True, q2=True, q3=False), run(q1=True, q2=True, q3=False), [s("q1", True), gone("q2"), s("q3", False)]]
    r = reliability(runs)
    assert r["questions"] == 2 and r["left_out_unavailable"] == ["q2"] and r["flaky"] == {}
    assert r["stable_correct"] == ["q1"] and r["stable_wrong"] == ["q3"] and r["accuracy_per_run"] == [0.5, 0.5, 0.5]
    assert bootstrap_mean_accuracy(runs)["questions"] == 2


def test_unanswerable_questions_the_llm_never_answered_are_not_flips():
    runs = [[s("U1", True, False, "refused_ok"), s("U2", True, False, "refused_ok")],
            [s("U1", True, False, "refused_ok"), gone("U2", False)]]
    u = unanswerable_stability(runs)
    assert u["questions"] == 1 and u["flipped"] == [] and u["refused_every_time"]["rate"] == 1.0


def test_a_comparison_leaves_out_the_questions_either_run_never_answered():
    a = [s("q1", True), s("q2", True), s("q3", False)]
    b = [s("q1", True), gone("q2"), s("q3", True)]
    c = compare(a, b)
    assert c["questions"] == 2 and c["left_out_unavailable"] == ["q2"] and c["only_b_correct"] == ["q3"] and c["only_a_correct"] == []


def test_an_unanswered_question_is_not_a_new_failure():
    baseline = [run(q1=True, q2=True), run(q1=True, q2=True)]
    assert new_failures(baseline, [s("q1", True), gone("q2")]) == []
    assert new_failures(baseline, run(q1=True, q2=False)) == ["q2"]


def test_text_stability_skips_questions_without_an_llm_answer():
    ok = {"id": "a", "text": "x", "status": "answered"}
    runs = [[ok, {"id": "b", "text": "y", "status": "answered"}], [ok, {"id": "b", "text": "", "status": "llm_unavailable"}]]
    assert text_stability(runs)["questions"] == 1


def test_the_stability_report_warns_loudly_about_an_incomplete_run(capsys):
    golden = load_golden()
    complete = [s(q.id, True) for q in golden.answerable]
    gappy = [gone(q.id) if k < 3 else s(q.id, True) for k, q in enumerate(golden.answerable)]
    rep = stability_report(golden, [complete, complete, gappy], [[], [], []], ["m"] * 3)
    assert rep["unavailable_per_run"] == [0, 0, 3] and rep["reliability"]["questions"] == len(golden.answerable) - 3
    answer_stats.print_stability(rep, golden)
    out = capsys.readouterr().out
    assert "WARNING: the LLM never answered some questions: run 3: 3" in out and "INCOMPLETE" in out and "cached" in out
    clean = stability_report(golden, [complete] * 3, [[], [], []], ["m"] * 3)
    answer_stats.print_stability(clean, golden)
    assert "WARNING" not in capsys.readouterr().out
