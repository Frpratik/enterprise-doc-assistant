"""Phase 4 Stage 1 tests: the answer key (evals/answers_golden.json) and the harder evaluation documents.
These protect the exam itself: a wrong key would make every later score meaningless."""
import copy

import pytest

from indexing.answer_golden import (Golden, Question, corpus_texts, fact_alternatives, fact_present, fact_recall, group_ranks,
                                    load_golden, summary, term_present, validate)
from indexing.search import Hit


@pytest.fixture(scope="module")
def golden(samples):
    from ingestion.config import PROJECT_ROOT
    import subprocess
    import sys
    if not (PROJECT_ROOT / "data" / "samples" / "eval2").exists():
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "make_eval_corpus.py")], check=True)
    return load_golden()


@pytest.fixture(scope="module")
def texts(golden):
    return corpus_texts(golden)


# ---------- the real answer key ----------
def test_the_answer_key_has_no_problems(golden, texts):
    """Every gold phrase exists in its document, every fact is in its sources and in the reference, and no document
    answers an 'unanswerable' question."""
    assert validate(golden, texts) == []


def test_the_exam_is_big_and_varied_enough(golden):
    types = {t: sum(q.type == t for q in golden.answerable) for t in {q.type for q in golden.answerable}}
    assert len(golden.answerable) >= 90 and len(golden.unanswerable) >= 45
    assert types["paraphrase"] >= 15 and types["table_lookup"] >= 10 and types["lookalike"] >= 10
    assert types["multi_chunk"] >= 4 and types["procedural"] >= 2 and types["yes_no"] >= 4 and types["computed"] >= 1
    assert {q.type for q in golden.unanswerable} == {"near_topic", "off_topic"}
    assert sum(q.difficulty == "hard" for q in golden.answerable) >= 15


def test_every_phase_3_question_is_still_in_the_exam(golden):
    """The old questions are kept (origin 'v1') so Phase 4 can show whether anything got worse since Phase 3."""
    import json
    from ingestion.config import PROJECT_ROOT
    old = json.loads((PROJECT_ROOT / "evals" / "unanswerable.json").read_text(encoding="utf-8"))
    old_questions = set(old["off_topic"]) | set(old["near_topic"])
    in_exam = {q.q for q in golden.questions}
    assert old_questions <= in_exam
    paraphrase = json.loads((PROJECT_ROOT / "evals" / "retrieval_golden.json").read_text(encoding="utf-8"))["questions"]
    assert {q["q"] for q in paraphrase} <= in_exam
    assert sum(q.origin == "v1" for q in golden.questions) == len(old_questions) + len(paraphrase)


def test_the_new_documents_are_hard_on_purpose(texts):
    """The look-alike facts that make the exam hard really are in different documents."""
    assert "within 15 days of returning" in texts["travel_policy.docx"] and "within 30 days of the date" in texts["expense_reimbursement_policy.pdf"]
    assert "within 2 hours" in texts["it_security_policy.pdf"] and "within 4 hours" in texts["it_security_policy.pdf"]
    assert "Rs 2,000" in texts["code_of_conduct_2026.pdf"] and "Rs 2,000" in texts["expense_reimbursement_policy.pdf"]
    assert "Rs 25,000" in texts["employee_benefits_guide.pdf"] and "Rs 25,000" in texts["employee_handbook.docx"]
    assert "Tier 2" in texts["travel_policy.docx"] and "Pune" in texts["travel_policy.docx"]           # an answer inside one table cell
    assert texts["it_security_policy.pdf"].count("ACME EXAMPLE") == 0                                    # repeated page headers were cleaned away


def test_every_document_in_the_exam_exists_once(golden):
    from indexing.answer_golden import corpus_paths
    paths = corpus_paths(golden.documents)
    assert len(paths) == 22 and len(set(p.name for p in paths.values())) == 22


# ---------- the checks catch mistakes (a validator that cannot fail protects nothing) ----------
def make(golden, **changes):
    g = copy.deepcopy(golden)
    q = next(x for x in g.questions if x.id == "A001")
    for k, v in changes.items():
        setattr(q, k, v)
    return g


def test_a_phrase_that_is_not_in_its_document_is_caught(golden, texts):
    bad = make(golden, sources=[[["leave_policy.pdf", "twelve thousand sick leaves"]]])
    assert any("is not in leave_policy.pdf" in p for p in validate(bad, texts))


def test_a_fact_that_is_not_in_the_sources_is_caught(golden, texts):
    bad = make(golden, facts=["12", "99 days"], reference="12 days or 99 days")
    assert any("'99 days' is not in its source documents" in p for p in validate(bad, texts))


def test_a_fact_missing_from_the_reference_answer_is_caught(golden, texts):
    bad = make(golden, facts=["12", "medical certificate"], reference="Employees get 12 sick leaves.")
    assert any("not in the reference answer" in p for p in validate(bad, texts))


def test_a_source_file_outside_the_document_list_is_caught(golden, texts):
    bad = make(golden, sources=[[["not_a_real_file.pdf", "x"]]])
    assert any("not in the document list" in p for p in validate(bad, texts))


def test_a_document_that_answers_an_unanswerable_question_is_caught(golden, texts):
    g = copy.deepcopy(golden)
    q = next(x for x in g.questions if not x.answerable)
    q.forbidden_terms = ["sick leaves"]                      # this phrase IS in the leave policy
    assert any("may be answerable" in p for p in validate(g, texts))


def test_missing_parts_and_unknown_types_are_caught(golden, texts):
    assert any("needs reference, facts and sources" in p for p in validate(make(golden, facts=[]), texts))
    assert any("unknown type" in p for p in validate(make(golden, type="trick"), texts))
    assert any("difficulty" in p for p in validate(make(golden, difficulty="impossible"), texts))
    assert any("at least 2 source groups" in p for p in validate(make(golden, type="multi_chunk"), texts))


def test_duplicate_questions_and_ids_are_caught(golden, texts):
    g = copy.deepcopy(golden)
    g.questions.append(copy.deepcopy(g.questions[0]))
    problems = validate(g, texts)
    assert any("duplicate question ids" in p for p in problems) and any("duplicate question:" in p for p in problems)


# ---------- the fact helpers (used by the answer scoring in Stage 2) ----------
def test_facts_with_alternative_wordings():
    assert fact_alternatives("12 sick leaves || 12 sick days") == ["12 sick leaves", "12 sick days"]
    assert fact_present("12 sick leaves || 12 sick days", "You get 12 sick days a year.")
    assert not fact_present("12 sick leaves || 12 sick days", "You get 8 casual leaves.")


def test_fact_matching_ignores_case_punctuation_and_number_formatting():
    assert fact_present("Rs 5,00,000", "The sum insured is RS 5,00,000.")
    assert fact_present("9:30", "Office opens at 9:30 AM")
    assert fact_present("half-day || half day", "counted as one half day of leave")
    assert fact_present("ethics@acme-example.com", "Write to ETHICS@acme-example.com.")


def test_fact_recall_is_the_share_of_facts_found():
    facts = ["26 weeks", "10 working days"]
    assert fact_recall(facts, "26 weeks of maternity and 10 working days of paternity leave") == 1.0
    assert fact_recall(facts, "Maternity leave is 26 weeks.") == 0.5
    assert fact_recall(facts, "I don't know") == 0.0
    assert fact_recall([], "anything") == 1.0


def test_numbers_are_not_matched_inside_other_numbers_by_accident():
    """'12' must not count as found inside '120'; the matching is on whole words."""
    assert not fact_present("12", "The limit is 120 days")
    assert fact_present("12", "The limit is 12 days")


def test_forbidden_terms_match_whole_words_only():
    assert not term_present("holi", "Diwali is a holiday")
    assert term_present("holi", "Is Holi a holiday?")
    assert term_present("wi-fi", "the guest Wi-Fi password")


# ---------- the evidence groups ----------
def hit(file_name, content):
    return Hit(1.0, "c", "d", 1, file_name, "H", {}, "text", content)


def test_group_ranks_find_the_best_rank_per_evidence_group():
    q = Question(id="x", q="x", type="multi_chunk", answerable=True, origin="v2", difficulty="hard",
                 sources=[[["a.pdf", "15 days"]], [["b.pdf", "30 days"], ["c.pdf", "30 days"]]])
    hits = [hit("z.pdf", "nothing"), hit("a.pdf", "within 15 days"), hit("c.pdf", "within 30 days"), hit("b.pdf", "30 days")]
    assert group_ranks(hits, q) == [2, 3]                       # the second group is satisfied by ANY alternative
    assert group_ranks(hits[:2], q) == [2, None]                # evidence missing -> None
    assert group_ranks([hit("a.pdf", "within 15 DAYS")], q)[0] == 1


def test_summary_mentions_the_counts(golden):
    s = summary(golden)
    assert "144 questions" in s and "answerable by type" in s and "v1 62, v2 82" in s
