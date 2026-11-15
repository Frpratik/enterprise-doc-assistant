"""Phase 4 Stage 2 tests: the rule-based answer scoring. Pure functions on hand-made records: no LLM, no database, no models."""
import pytest

from indexing.answer_golden import Golden, Question
from indexing.answer_score import (evidence_groups, format_report, is_hedged, rate, score_all, score_record, strip_citations,
                                   summarize, token_f1, wilson)


# ---------- helpers: a tiny answer key and hand-made records ----------
def chunk(n, file_name, content):
    return {"file": file_name, "heading": "H", "chunk_id": f"c{n}", "content": content}


TRAVEL = chunk(1, "travel_policy.docx", "Travel claims must be submitted within 15 days of returning from the trip.")
EXPENSE = chunk(2, "expense_policy.pdf", "General expense claims must be submitted within 30 days of the date of the expense.")
NOISE = chunk(3, "other.md", "Nothing useful here.")


def answerable(**kw):
    base = dict(id="A1", q="By when must I submit my travel claim?", type="lookalike", answerable=True, origin="v2", difficulty="medium",
                reference="Within 15 days of returning from the trip.", facts=["15 days"],
                sources=[[["travel_policy.docx", "within 15 days of returning"]]], wrong_facts=["30 days"])
    base.update(kw)
    return Question(**base)


def unanswerable(**kw):
    base = dict(id="U1", q="What is the penalty for a late claim?", type="near_topic", answerable=False, origin="v2",
                reference="NOT_IN_DOCUMENTS", forbidden_terms=["penalty"])
    base.update(kw)
    return Question(**base)


def record(status="answered", text="Within 15 days of returning [1].", context=None, citations=(1,), **kw):
    context = [TRAVEL, EXPENSE, NOISE] if context is None else context
    return {"id": "A1", "status": status, "text": text, "context": context, "latency_ms": 800,
            "citations": [{"n": n, "chunk_id": context[n - 1]["chunk_id"]} for n in citations], **kw}


# ---------- text helpers ----------
@pytest.mark.parametrize("text", [
    "The excerpts do not state how long this takes [1].",
    "The policy does not specify a penalty.",
    "This is not mentioned in the documents.",
    "There is no information about this.",
    "I cannot find the answer in the excerpts.",
    "The provided excerpts do not state how to reset the password, only that passwords change every 90 days [1].",
    "It is unclear from the text.",
    "The documents don't contain details on this."])
def test_hedges_are_detected(text):
    assert is_hedged(text)


@pytest.mark.parametrize("text", [
    "The notice period for all permanent employees is 60 days [1].",
    "No. Business class is for grade L3 and above [1].",
    "No, a receipt is mandatory only above Rs 500 [1].",
    "Maternity leave is 26 weeks [1] and paternity leave is 10 working days [1].",
    "Employees must not accept gifts worth more than Rs 2,000 [1].",
    "The company does not allow USB drives by default [1]."])
def test_normal_answers_are_not_hedges(text):
    assert not is_hedged(text)


def test_citation_markers_are_stripped_and_do_not_count_as_words():
    assert strip_citations("12 days [1][2] a year [3].") == "12 days  a year ."
    assert token_f1("Within 15 days of returning from the trip [1].", "Within 15 days of returning from the trip.") == 1.0


def test_token_f1_is_a_rough_overlap_score():
    assert token_f1("completely different words", "Within 15 days") == 0.0
    assert 0.3 < token_f1("15 days", "Within 15 days of returning from the trip.") < 0.7
    assert token_f1("", "x") == 0.0


def test_wilson_interval_behaves():
    lo, hi = wilson(9, 10)
    assert 0.55 < lo < 0.65 and 0.97 < hi <= 1.0
    assert wilson(0, 0) == (0.0, 1.0)
    lo0, hi0 = wilson(0, 20)
    assert lo0 == 0.0 and 0.1 < hi0 < 0.2                          # 0 of 20 does NOT mean "0% for sure"
    assert wilson(50, 100)[1] - wilson(50, 100)[0] < wilson(5, 10)[1] - wilson(5, 10)[0]       # more data, narrower interval
    assert rate(3, 4)["rate"] == 0.75 and rate(0, 0)["rate"] is None


# ---------- answerable questions ----------
def test_a_correct_answer():
    s = score_record(answerable(), record())
    assert s["outcome"] == "correct" and s["cause"] == "-" and s["fact_recall"] == 1.0 and s["wrong_fact_hits"] == []
    assert s["evidence_in_context"] is True and s["cites_right"] is True and s["citation_precision"] == 1.0 and not s["hedged"]


def test_a_lookalike_value_next_to_the_right_fact_is_flagged():
    s = score_record(answerable(), record(text="Within 15 days for travel; general claims take 30 days [1][2].", citations=(1, 2)))
    assert s["outcome"] == "lookalike_mention" and s["wrong_fact_hits"] == ["30 days"] and s["cause"] == "generation"


def test_the_wrong_lookalike_answer_is_incorrect_and_blamed_on_generation_when_the_evidence_was_given():
    s = score_record(answerable(), record(text="Within 30 days [2].", citations=(2,)))
    assert s["outcome"] == "incorrect" and s["cause"] == "generation" and s["facts_missing"] == ["15 days"]
    assert s["wrong_fact_hits"] == ["30 days"] and s["cites_right"] is False and s["citation_precision"] == 0.0


def test_a_wrong_answer_is_blamed_on_retrieval_when_the_evidence_never_reached_the_llm():
    s = score_record(answerable(), record(text="Within 30 days [1].", context=[EXPENSE, NOISE]))
    assert s["outcome"] == "incorrect" and s["cause"] == "retrieval" and s["evidence_in_context"] is False


def test_refusals_of_answerable_questions_get_a_cause():
    gate = score_record(answerable(), record(status="refused_gate", citations=()))
    assert (gate["outcome"], gate["cause"]) == ("refused", "gate") and not gate["answered"]
    llm_with_evidence = score_record(answerable(), record(status="refused_llm", citations=()))
    assert (llm_with_evidence["outcome"], llm_with_evidence["cause"]) == ("refused", "generation")
    llm_without = score_record(answerable(), record(status="refused_llm", citations=(), context=[NOISE]))
    assert (llm_without["outcome"], llm_without["cause"]) == ("refused", "retrieval")
    uncited = score_record(answerable(), record(status="uncited", citations=()))
    assert uncited["outcome"] == "uncited" and uncited["cause"] == "generation"


def test_unavailable_and_crashed_questions_are_not_counted_as_wrong_answers():
    assert score_record(answerable(), record(status="llm_unavailable", citations=()))["outcome"] == "unavailable"
    assert score_record(unanswerable(), {"id": "U1", "status": "error", "text": "", "context": [], "citations": []})["outcome"] == "unavailable"


def test_a_multi_chunk_question_needs_every_evidence_group():
    q = answerable(type="multi_chunk", facts=["15 days", "30 days"], wrong_facts=[], reference="15 days and 30 days",
                   sources=[[["travel_policy.docx", "within 15 days of returning"]], [["expense_policy.pdf", "within 30 days of the date"]]])
    both = score_record(q, record(text="Travel 15 days [1], general 30 days [2].", citations=(1, 2)))
    assert both["outcome"] == "correct" and both["citation_recall"] == 1.0 and both["evidence_groups"] == "2/2"
    only_one = score_record(q, record(text="Travel 15 days and general 30 days [1].", citations=(1,)))
    assert only_one["outcome"] == "correct" and only_one["citation_recall"] == 0.5 and only_one["cites_right"] is False
    missing = score_record(q, record(text="Travel 15 days [1].", context=[TRAVEL, NOISE], citations=(1,)))
    assert missing["outcome"] == "incorrect" and missing["cause"] == "retrieval" and missing["evidence_groups"] == "1/2"


def test_citation_precision_counts_useful_sources():
    s = score_record(answerable(), record(text="Within 15 days [1][3].", citations=(1, 3)))
    assert s["citation_precision"] == 0.5 and s["cites_right"] is True


def test_evidence_matching_needs_the_right_file_and_whole_words():
    q = answerable()
    assert evidence_groups([TRAVEL], q) == [True]
    assert evidence_groups([chunk(9, "wrong_file.docx", TRAVEL["content"])], q) == [False]
    assert evidence_groups([chunk(9, "travel_policy.docx", "within 115 days of returning")], q) == [False]


def test_a_hedged_answer_is_marked_even_when_it_has_the_fact():
    s = score_record(answerable(), record(text="The excerpts do not state the penalty, but claims are due within 15 days [1]."))
    assert s["hedged"] is True and s["outcome"] == "correct"


# ---------- unanswerable questions ----------
def test_unanswerable_outcomes():
    assert score_record(unanswerable(), record(status="refused_gate", citations=()))["outcome"] == "refused_ok"
    gate = score_record(unanswerable(), record(status="refused_gate", citations=()))
    assert gate["cause"] == "gate"
    assert score_record(unanswerable(), record(status="refused_llm", citations=()))["cause"] == "llm"
    false = score_record(unanswerable(), record(text="There is a 5% penalty [1]."))
    assert false["outcome"] == "false_answer" and false["answered"]
    soft = score_record(unanswerable(), record(text="The excerpts do not state a penalty, only the 15 day deadline [1]."))
    assert soft["outcome"] == "soft_refusal"


# ---------- the summary ----------
def make_scored():
    golden = Golden(1, [], [answerable(id="A1"), answerable(id="A2", type="single_fact", difficulty="easy"),
                            answerable(id="A3"), unanswerable(id="U1"), unanswerable(id="U2", type="off_topic"), unanswerable(id="U3")])
    records = [
        {**record(), "id": "A1"},                                                                         # correct
        {**record(text="Within 30 days [2].", citations=(2,)), "id": "A2"},                                  # incorrect (generation)
        {**record(status="refused_gate", citations=()), "id": "A3"},                                         # refused by gate
        {**record(status="refused_gate", citations=()), "id": "U1"},                                         # ok
        {**record(status="refused_gate", citations=()), "id": "U2"},                                         # ok
        {**record(text="A 5% penalty [1]."), "id": "U3"}]                                                    # false answer
    return golden, records


def test_summary_counts_rates_and_confusion_matrix():
    golden, records = make_scored()
    s = summarize(score_all(golden, records))
    assert s["n_answerable"] == 3 and s["n_unanswerable"] == 3
    assert s["answerable"]["accuracy"]["k"] == 1 and s["answerable"]["accuracy"]["rate"] == pytest.approx(1 / 3)
    assert s["answerable"]["incorrect"]["k"] == 1 and s["answerable"]["refused"]["k"] == 1
    assert s["unanswerable"]["refused_ok"]["k"] == 2 and s["unanswerable"]["false_answer"]["k"] == 1
    assert s["confusion"]["should_answer"] == {"answered": 2, "refused": 1, "unavailable": 0}
    assert s["confusion"]["should_refuse"] == {"answered": 1, "refused": 2, "unavailable": 0}
    assert s["why_not_correct"] == {"incorrect / generation": 1, "refused / gate": 1}
    assert s["by_type"]["lookalike"]["n"] == 2 and s["by_difficulty"]["easy"]["n"] == 1
    assert s["unanswerable"]["by_type"]["off_topic"]["refused_by_gate"] == 1
    assert s["evidence_in_context"]["k"] == 3 and s["llm_latency_ms"]["median"] == 800


def test_the_report_names_every_failure():
    golden, records = make_scored()
    scored = score_all(golden, records)
    text = format_report(summarize(scored), scored, golden)
    assert "ANSWERABLE QUESTIONS NOT CORRECT (2)" in text and "A2" in text and "A3" in text
    assert "UNANSWERABLE QUESTIONS THAT WERE ANSWERED (1)" in text and "U3" in text and "FALSE ANSWER" in text


def test_scoring_ignores_records_for_questions_not_in_the_key_and_works_from_saved_json():
    import json
    golden, records = make_scored()
    again = json.loads(json.dumps(records + [{"id": "ZZZ", "status": "answered", "text": "x", "context": [], "citations": []}]))
    scored = score_all(golden, again)
    assert [s["id"] for s in scored] == ["A1", "A2", "A3", "U1", "U2", "U3"]           # re-scoring needs only the saved records


# ---------- scoring must not depend on how a model writes numbers or starts a yes/no answer ----------
def test_an_answer_with_spelled_out_numbers_scores_the_same_as_one_with_digits():
    q = answerable(q="Who is covered?", facts=["2 children"], wrong_facts=[], reference="The spouse and up to 2 children.",
                   sources=[[["travel_policy.docx", "within 15 days of returning"]]])
    digits = score_record(q, record(text="The spouse and up to 2 children [1]."))
    words = score_record(q, record(text="The spouse and up to two children [1]."))
    assert digits["outcome"] == words["outcome"] == "correct" and digits["fact_recall"] == words["fact_recall"] == 1.0


def test_a_yes_no_question_is_scored_on_the_polarity_not_on_a_detail_it_did_not_ask_for():
    q = answerable(q="Do I need a receipt for Rs 300?", type="yes_no", facts=["@no || not required"], wrong_facts=[],
                   reference="No. Receipts are mandatory only above Rs 500.")
    assert score_record(q, record(text="No, a receipt is not required [1]."))["outcome"] == "correct"
    assert score_record(q, record(text="Yes, you need a receipt [1]."))["outcome"] == "incorrect"


# ---------- an incomplete run says so ----------
def test_unanswered_questions_are_counted_and_the_report_warns():
    golden, records = make_scored()
    records[1] = {**record(status="llm_unavailable", citations=()), "id": "A2"}
    records[4] = {"id": "U2", "status": "error", "text": "", "context": [], "citations": []}
    scored = score_all(golden, records)
    s = summarize(scored)
    assert s["unavailable"] == 2
    text = format_report(s, scored, golden)
    assert "WARNING: the LLM never answered 2 questions" in text and "INCOMPLETE" in text and "cached" in text


def test_a_complete_run_has_no_warning():
    golden, records = make_scored()
    scored = score_all(golden, records)
    assert summarize(scored)["unavailable"] == 0 and "WARNING" not in format_report(summarize(scored), scored, golden)
