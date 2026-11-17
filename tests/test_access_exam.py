"""Phase 5 Stage 5 tests: the access exam itself (can it see a leak? can it see over-blocking?) and its place in the regression gate."""
import json

import numpy as np
import pytest

from indexing import access_exam, answer as answer_mod, eval_gate, rerank as rerank_mod, search as search_mod, sync
from indexing.access import Principal
from indexing.access_exam import ExamQuestion, ExamResult, Person, excerpts_of, run_exam
from indexing.answer import build_messages
from indexing.answer_golden import load_golden
from indexing.embeddings import Embedder
from indexing.eval_gate import NOT_RUN, Gate, evaluate, format_gate
from indexing.search import Hit
from ingestion.config import settings

from test_answer_gate import BASELINE, GOLDEN, run


# ---------------- a small world: 4 documents, 4 people, fake embeddings, no model download ----------------
class FakeEmbedder(Embedder):
    name, dims = "fake-model", 26

    def embed_documents(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


DOCS = {"pub.md": ("acme", ["all"], "CANARY-PUB-1"), "hr.md": ("acme", ["hr"], "CANARY-HR-2"), "admin.md": ("acme", ["admin"], "CANARY-ADMIN-3"),
        "globex.md": ("globex", ["all"], "CANARY-GLOBEX-4")}
QUESTIONS = [ExamQuestion("Qpub", "what is the salary policy payroll bonus rule", "acme", [[("pub.md", "CANARY-PUB-1")]]),
             ExamQuestion("Qhr", "what is the salary policy payroll bonus rule for hr", "acme", [[("hr.md", "CANARY-HR-2")]]),
             ExamQuestion("Qadmin", "what is the salary policy payroll bonus rule for admins", "acme", [[("admin.md", "CANARY-ADMIN-3")]]),
             ExamQuestion("Qglobex", "what is the salary policy payroll bonus rule at globex", "globex", [[("globex.md", "CANARY-GLOBEX-4")]])]
PEOPLE = [Person("anna", Principal("acme", (), "a")), Person("hana", Principal("acme", ("hr",), "h")), Person("adam", Principal("acme", ("admin",), "d")),
          Person("greg", Principal("globex", (), "g"))]


@pytest.fixture
def world(db_url, temp_data, tmp_path, monkeypatch):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [{"match": n, "tenant": t, "roles": r} for n, (t, r, _) in DOCS.items()]}), encoding="utf-8")
    monkeypatch.setattr(settings, "access_rules_path", rules)
    src = temp_data / "source"
    src.mkdir()
    for name, (_, _, canary) in DOCS.items():
        (src / name).write_text(f"# {name} Handbook\n\n## Pay\nThe salary policy and payroll bonus rules. Reference {canary}.\n", encoding="utf-8")
    emb = FakeEmbedder()
    sync.run_sync(src, embedder=emb)
    monkeypatch.setattr(search_mod, "get_embedder", lambda: emb)
    monkeypatch.setattr(rerank_mod, "score_pairs", lambda q, texts: [5.0] * len(texts))
    return emb


# ---------------- the exam on a correct system ----------------
def test_a_correct_system_passes_and_the_counts_are_exactly_what_they_should_be(world):
    res = run_exam(QUESTIONS, PEOPLE, embedder=world)
    assert res.ok and res.problems == [] and res.leaks == [] and res.over_blocks == []
    assert (res.questions, res.people, res.checks) == (4, 4, 16)
    assert (res.served, res.denied) == (6, 10)                         # anna 1+3, hana 2+2, adam 2+2, greg 1+3
    assert res.per_person["hana"] == {"asked": 4, "served": 2, "denied": 2, "leaks": 0}
    assert res.per_person["greg"] == {"asked": 4, "served": 1, "denied": 3, "leaks": 0}


def test_the_report_says_what_was_checked(world):
    text = access_exam.format_result(run_exam(QUESTIONS, PEOPLE, embedder=world))
    assert "ACCESS EXAM: PASS" in text and "4 questions x 4 people = 16 checks" in text and "served correctly: 6   withheld correctly: 10   leaks: 0" in text and "hana" in text


# ---------------- the exam must be able to FAIL ----------------
def test_without_the_filter_the_exam_finds_every_leak_on_every_surface(world, monkeypatch):
    monkeypatch.setattr(search_mod, "ACCESS_SQL", "true")              # the access condition removed from both searches
    res = run_exam(QUESTIONS, PEOPLE, embedder=world)
    assert not res.ok and res.leaks
    anna = [l for l in res.leaks if l["person"] == "anna"]
    assert {l["surface"] for l in anna} >= {"prompt"}                  # the model was handed documents anna may not read
    assert {"CANARY-HR-2", "CANARY-ADMIN-3", "CANARY-GLOBEX-4"} <= {l["value"] for l in anna if l["kind"] == "canary"}
    assert {"hr.md", "admin.md", "globex.md"} <= {l["value"] for l in anna if l["kind"] == "file name"}
    assert any(l["kind"] == "evidence" for l in anna)
    assert {p["check"] for p in res.problems} >= {"forbidden_evidence"}
    greg = {l["value"] for l in res.leaks if l["person"] == "greg"}
    assert "pub.md" in greg and "CANARY-PUB-1" in greg                 # another company's documents too


def test_a_leak_into_only_the_answer_or_the_citations_is_also_found(world, monkeypatch):
    """The filter is fine, but something later shows a forbidden file: only the surfaces after the prompt are wrong."""
    real = access_exam.answer_stream

    def leaky(question, **kw):
        for event, data in real(question, **kw):
            if event == "done":
                data = {**data, "citations": data["citations"] + [{"n": 9, "file_name": "admin.md", "heading_path": "x", "where": "", "chunk_id": "c"}]}
            yield event, data
    monkeypatch.setattr(access_exam, "answer_stream", leaky)
    res = run_exam(QUESTIONS, PEOPLE, embedder=world)
    assert not res.ok and {l["surface"] for l in res.leaks} == {"citations", "events"} and {l["value"] for l in res.leaks} == {"admin.md"}
    assert "adam" not in {l["person"] for l in res.leaks}              # adam may read admin.md: not a leak for him


def test_hiding_everything_is_not_a_pass(world, monkeypatch):
    monkeypatch.setattr(search_mod, "ACCESS_SQL", "false")
    res = run_exam(QUESTIONS, PEOPLE, embedder=world)
    assert res.leaks == [] and res.served == 0 and not res.ok
    assert [p["check"] for p in res.problems] == ["access_exam_vacuous"]


def test_a_filter_that_wrongly_drops_a_role_is_over_blocking(world, monkeypatch):
    """A person who may read a document but does not get it, while the all-roles auditor does: not a leak, but still a failure."""
    broken = ("(d.access_status = 'labelled' AND d.tenant_id = %(tenant)s AND d.allowed_roles && %(roles)s::text[] "
              "AND (d.allowed_roles = ARRAY['all']::text[] OR cardinality(%(roles)s::text[]) >= 7))")
    monkeypatch.setattr(search_mod, "ACCESS_SQL", broken)
    res = run_exam(QUESTIONS, PEOPLE, embedder=world)
    assert res.leaks == [] and {(o["person"], o["question"]) for o in res.over_blocks} == {("hana", "Qhr"), ("adam", "Qadmin")}
    assert {p["check"] for p in res.problems} == {"access_over_block"} and not res.ok


def test_an_exam_that_asked_nothing_is_a_failure():
    assert [p["check"] for p in ExamResult().problems] == ["access_exam_vacuous"]


def test_a_name_that_a_readable_document_also_contains_is_not_a_leak(world, monkeypatch, temp_data):
    """pub.md mentions 'hr.md' in its text: anna may legitimately see that string, because a document she CAN read contains it."""
    src = temp_data / "source"
    (src / "pub.md").write_text("# pub.md Handbook\n\n## Pay\nThe salary policy and payroll bonus rules, see also hr.md. Reference CANARY-PUB-1.\n", encoding="utf-8")
    sync.run_sync(src, embedder=world)
    res = run_exam(QUESTIONS[:1], PEOPLE[:1], embedder=world)
    assert res.ok is False and "vacuous" in res.problems[0]["check"]   # (only one person: nothing denied) ...
    assert res.leaks == []                                             # ... but no leak is reported for the name that is in her own document


# ---------------- the pieces ----------------
def test_the_exam_reads_the_excerpts_the_way_the_prompt_writes_them():
    hits = [Hit(0.9, "c1", "d1", 1, "leave_policy.pdf", "Leave > Sick", {"pages": [1]}, "text", "You get 12 sick leaves."),
            Hit(0.8, "c2", "d2", 1, "globex_leave_policy.md", "Globex > Sick", {}, "text", "Globex gives 20 sick leaves.\nSecond line.")]
    blocks = excerpts_of(build_messages("how many sick leaves?", hits))
    assert [b[0] for b in blocks] == ["leave_policy.pdf", "globex_leave_policy.md"] and "20 sick leaves" in blocks[1][1]
    assert excerpts_of([{"content": "s"}, {"content": "no excerpts at all"}]) == []


def test_the_exam_questions_cover_every_restricted_document_and_both_companies():
    qs = access_exam.build_questions(load_golden())
    assert len(qs) == 24 and len({q.id for q in qs}) == 24
    files = {f for q in qs for g in q.groups for f, _ in g}
    assert {"salary_structure.xlsx", "salary_revision_matrix.xlsx", "mixed_quarterly_report.pdf", "vendor_onboarding_sop.docx", "acme_board_minutes.md",
            "acme_executive_compensation.md", "globex_leave_policy.md", "globex_salary_bands.md", "leave_policy.pdf"} <= files
    assert {q.tenant for q in qs} == {"acme", "globex"}
    assert {p.principal.tenant for p in access_exam.PEOPLE} == {"acme", "globex"} and len(access_exam.PEOPLE) == 10


# ---------------- the gate ----------------
def clean():
    return ExamResult(questions=24, people=10, checks=240, served=93, denied=147)


def gate_with(access):
    scored, records = run()
    return evaluate(BASELINE, GOLDEN, scored, records, set(), access=access)


def test_the_answer_rules_alone_are_unchanged_when_no_access_exam_is_part_of_the_call():
    assert gate_with(NOT_RUN).verdict == "PASS"


def test_a_clean_access_exam_passes_the_gate_and_is_shown_in_the_report():
    g = gate_with(clean())
    assert g.verdict == "PASS" and g.info["access"] == {"checks": 240, "served": 93, "denied": 147, "leaks": 0}
    assert "access exam: 240 checks, 93 served correctly, 147 withheld correctly, 0 leaks" in format_gate(g)


def test_one_leak_fails_the_gate_whatever_the_answers_are():
    res = clean()
    res.leaks = [{"person": "acme employee", "question": "A1", "surface": "prompt", "kind": "canary", "value": "CANARY-HR-2"}]
    g = gate_with(res)
    assert g.verdict == "FAIL" and [(f["check"], f["id"]) for f in g.failures] == [("forbidden_evidence", "A1")]
    assert "acme employee" in g.failures[0]["detail"] and "CANARY-HR-2" in g.failures[0]["detail"] and g.failures[0]["question"] == "question A1?"


def test_over_blocking_and_a_vacuous_exam_fail_the_gate():
    res = clean()
    res.over_blocks = [{"person": "acme hr", "question": "X001"}]
    assert {f["check"] for f in gate_with(res).failures} == {"access_over_block"}
    assert {f["check"] for f in gate_with(ExamResult(questions=1, people=1, checks=1)).failures} == {"access_exam_vacuous"}


def test_a_skipped_access_exam_makes_the_gate_incomplete_never_a_pass():
    g = gate_with(None)
    assert g.verdict == "INCOMPLETE" and g.exit_code == 2 and g.unverified[0]["why"] == "the access exam was not run"
    assert "access exam was not run" in format_gate(g)


def test_a_leak_still_fails_even_when_other_things_are_unverified():
    res = clean()
    res.leaks = [{"person": "p", "question": "A1", "surface": "events", "kind": "file name", "value": "x.pdf"}]
    scored, records = run()
    g = evaluate(BASELINE, GOLDEN, scored, records, {"question A2?"}, access=res)
    assert g.verdict == "FAIL" and g.unverified


@pytest.fixture
def stub_run(monkeypatch):
    from indexing import safety_exam
    calls = []
    monkeypatch.setattr(access_exam, "run_full", lambda progress=None: calls.append(1) or clean())
    monkeypatch.setattr(safety_exam, "run", lambda mode="replay", progress=None: safety_exam.SafetyResult(offline=True))     # these tests are about the access exam: no real safety run
    monkeypatch.setattr(eval_gate.answer_run, "generate_repeats", lambda *a, **k: [run()[1]])
    monkeypatch.setattr(eval_gate, "score_all", lambda golden, records: run()[0])
    return calls


def test_run_gate_runs_the_access_exam_unless_told_otherwise(stub_run):
    assert eval_gate.run_gate(BASELINE, GOLDEN).verdict == "PASS" and stub_run == [1]
    assert eval_gate.run_gate(BASELINE, GOLDEN, access=True).verdict == "PASS" and stub_run == [1, 1]


def test_a_quick_ids_check_does_not_run_the_exam_but_skipping_it_on_purpose_is_incomplete(stub_run):
    assert eval_gate.run_gate(BASELINE, GOLDEN, ids=["A1"]).verdict == "PASS" and stub_run == []
    assert eval_gate.run_gate(BASELINE, GOLDEN, access=False).verdict == "INCOMPLETE" and stub_run == []


def test_the_command_has_a_skip_access_flag(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr("indexing.db.can_connect", lambda *a, **k: True)
    monkeypatch.setattr(eval_gate, "load_baseline", lambda *a, **k: BASELINE)
    monkeypatch.setattr(eval_gate, "load_golden", lambda *a, **k: GOLDEN)
    monkeypatch.setattr(eval_gate, "run_gate", lambda *a, **k: seen.append(k.get("access")) or Gate())
    monkeypatch.setattr(eval_gate, "format_gate", lambda g: "x")
    assert eval_gate.main([]) == 0 and eval_gate.main(["--skip-access"]) == 0
    assert seen == [None, False]
