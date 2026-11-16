"""Phase 5 Stage 2 tests: the access filter inside every search path. A person only ever reads chunks of documents they may see."""
import json
import os

import numpy as np
import pytest

from indexing import access, retrieve as retrieve_mod, search as search_mod, sync
from indexing.access import ALL, ROLES, AccessError, Principal
from indexing.db import connect
from indexing.embeddings import Embedder
from indexing.search import search
from ingestion.config import settings

ORIGINAL_SEARCH = search_mod.search      # taken at import time, before conftest wraps `search` with a default asker for the older tests
MODES = ["vector", "keyword", "hybrid"]


class FakeEmbedder(Embedder):
    name, dims = "fake-model", 26

    def embed_documents(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


RULES = [("pub.md", "acme", ["all"]), ("hr.md", "acme", ["hr"]), ("fin.md", "acme", ["finance", "hr"]), ("admin.md", "acme", ["admin"]),
         ("globex.md", "globex", ["all"])]                                         # stray.md has no rule: unlabelled
CANARY = {"pub.md": "CANARYPUB", "hr.md": "CANARYHR", "fin.md": "CANARYFIN", "admin.md": "CANARYADMIN", "globex.md": "CANARYGLOBEX", "stray.md": "CANARYSTRAY"}


@pytest.fixture
def corpus(db_url, temp_data, tmp_path, monkeypatch):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [{"match": m, "tenant": t, "roles": r} for m, t, r in RULES]}), encoding="utf-8")
    monkeypatch.setattr(settings, "access_rules_path", rules)
    src = temp_data / "source"
    src.mkdir()
    for name, canary in CANARY.items():
        (src / name).write_text(f"# {name}\n\n## Pay\nThe salary policy and payroll bonus rules. Reference {canary}.\n", encoding="utf-8")
    emb = FakeEmbedder()
    sync.run_sync(src, embedder=emb)
    return emb


def seen(emb, who, mode, query="salary policy payroll bonus"):
    hits = search(query, k=50, embedder=emb, mode=mode, candidates=50, principal=who)
    return {h.file_name for h in hits}


def acme(*roles):
    return Principal("acme", roles)


# ---------------- who is asking ----------------
def test_a_principal_validates_company_and_roles_and_all_is_not_a_role_a_person_can_hold():
    assert Principal("acme", ("HR", "finance")).roles == ("finance", "hr")
    assert Principal("acme", ()).roles == ()                                      # no special role: only documents for everyone
    for bad in (lambda: Principal("Acme", ("hr",)), lambda: Principal("acme", ("boss",)), lambda: Principal("acme", ("all",)),
                lambda: Principal("unassigned", ("hr",))):
        with pytest.raises(AccessError):
            bad()


def test_an_auditor_holds_every_role_of_one_company_only():
    a = Principal.auditor("globex")
    assert set(a.roles) == set(ROLES) and a.tenant == "globex"
    assert a.sql_params() == {"tenant": "globex", "roles": [*ROLES, ALL]}


def test_a_search_without_a_principal_is_refused_not_answered_with_everything(corpus):
    for who in (None, "acme", {"tenant": "acme"}, ("acme", ["hr"])):
        with pytest.raises(AccessError, match="needs a principal"):
            ORIGINAL_SEARCH("salary", embedder=corpus, principal=who)


# ---------------- the visibility matrix, in every search mode ----------------
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("who, expected", [
    (acme(), {"pub.md"}),                                                         # nobody special: only what is for everyone
    (acme("hr"), {"pub.md", "hr.md", "fin.md"}),
    (acme("finance"), {"pub.md", "fin.md"}),
    (acme("admin"), {"pub.md", "admin.md"}),                                      # admin is NOT a super-role: no hr.md, no fin.md
    (acme("it", "manager"), {"pub.md"}),                                          # roles that match nothing
    (acme("hr", "finance", "admin"), {"pub.md", "hr.md", "fin.md", "admin.md"}),
    (Principal("globex", ("hr",)), {"globex.md"}),                                # another company never sees acme, and acme never sees globex
    (Principal.auditor("acme"), {"pub.md", "hr.md", "fin.md", "admin.md"}),
    (Principal("nobody-co", ("hr",)), set()),                                     # a company with no documents
])
def test_each_person_sees_exactly_the_documents_they_may(corpus, mode, who, expected):
    assert seen(corpus, who, mode) == expected                                    # and never stray.md: unlabelled is invisible to everyone


@pytest.mark.parametrize("mode", MODES)
def test_no_restricted_text_reaches_a_person_who_may_not_read_it(corpus, mode):
    for who in (acme(), acme("finance"), acme("admin"), Principal("globex", ())):
        hits = search("salary policy payroll bonus Reference", k=50, embedder=corpus, mode=mode, candidates=50, principal=who)
        text = " ".join(h.content for h in hits)
        allowed = {n for n in seen(corpus, who, mode)}
        for name, canary in CANARY.items():
            assert (canary in text) == (name in allowed), (who, name)


def test_the_same_holds_for_the_exact_word_search(corpus):
    # a canary is an exact term: keyword search finds it only for people who may read its document
    assert [h.file_name for h in search("CANARYHR", k=5, embedder=corpus, mode="keyword", principal=acme("hr"))] == ["hr.md"]
    for who in (acme(), acme("finance", "admin"), Principal("globex", ("hr",))):
        assert search("CANARYHR", k=5, embedder=corpus, mode="keyword", principal=who) == []
    assert search("CANARYSTRAY", k=5, embedder=corpus, mode="keyword", principal=Principal.auditor("acme")) == []    # unlabelled: nobody


# ---------------- labels take effect at once ----------------
def doc_id_of(name):
    with connect() as conn:
        return conn.execute("SELECT doc_id FROM documents WHERE file_name = %s", (name,)).fetchone()[0]


def test_a_label_change_changes_the_next_search_without_re_indexing(corpus):
    assert "hr.md" not in seen(corpus, acme("finance"), "hybrid")
    access.set_access(doc_id_of("hr.md"), "acme", "finance")
    assert "hr.md" in seen(corpus, acme("finance"), "hybrid") and "hr.md" not in seen(corpus, acme("hr"), "hybrid")
    access.set_access(doc_id_of("hr.md"), "acme", "", unlabel=True)               # the panic button
    assert all("hr.md" not in seen(corpus, w, m) for w in (acme("finance"), acme("hr"), Principal.auditor("acme")) for m in MODES)
    access.set_access(doc_id_of("stray.md"), "acme", "all")                       # an unlabelled document becomes visible once labelled
    assert "stray.md" in seen(corpus, acme(), "hybrid")


def test_a_deleted_document_stays_invisible_whatever_its_label(corpus):
    with connect() as conn:
        conn.execute("UPDATE documents SET deleted_at = now() WHERE file_name = 'pub.md'")
    assert "pub.md" not in seen(corpus, Principal.auditor("acme"), "hybrid")


# ---------------- enough results for people who may see little ----------------
def test_a_person_with_little_access_still_gets_results_when_most_chunks_are_restricted(db_url, temp_data, tmp_path, monkeypatch):
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [{"match": "secret*.md", "tenant": "acme", "roles": ["admin"]},
                                                          {"match": "open.md", "tenant": "acme", "roles": ["all"]}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "access_rules_path", rules)
    src = temp_data / "source"
    src.mkdir()
    for i in range(60):                                                           # 60 restricted documents that look exactly like the question
        (src / f"secret{i}.md").write_text(f"# secret{i}\n\n## S\nsalary policy payroll bonus rules number {i}.\n", encoding="utf-8")
    (src / "open.md").write_text("# open\n\n## S\nsalary policy payroll bonus rules for everyone.\n", encoding="utf-8")
    emb = FakeEmbedder()
    sync.run_sync(src, embedder=emb)
    for mode in MODES:
        hits = search("salary policy payroll bonus rules", k=3, embedder=emb, mode=mode, candidates=5, principal=acme())
        assert [h.file_name for h in hits] == ["open.md"], mode
    assert len(search("salary policy payroll bonus rules", k=10, embedder=emb, mode="vector", candidates=10, principal=acme("admin"))) == 10


class Recorder:
    """Wraps a connection and records every SQL statement."""
    def __init__(self, conn, log):
        self.conn, self.log = conn, log

    def execute(self, sql, *a, **k):
        self.log.append(str(sql))
        return self.conn.execute(sql, *a, **k)


def test_the_vector_search_turns_on_iterative_scan_so_filtering_cannot_starve_it(corpus):
    log = []
    with connect() as conn:
        search_mod._vector_candidates(Recorder(conn, log), corpus.embed_query("salary"), corpus, 5, acme())
    assert any("hnsw.iterative_scan = strict_order" in s for s in log) and any("hnsw.max_scan_tuples" in s for s in log)


def test_every_search_query_carries_the_access_condition(corpus):
    log = []
    with connect() as conn:
        qv = corpus.embed_query("salary")
        search_mod._vector_candidates(Recorder(conn, log), qv, corpus, 5, acme())
        search_mod._keyword_candidates(Recorder(conn, log), "salary policy", qv, corpus, 5, acme())
    queries = [s for s in log if "FROM" in s]
    assert len(queries) == 2 and all(q.count("d.access_status = 'labelled'") >= 1 for q in queries)
    assert queries[1].count("d.access_status = 'labelled'") == 2                  # the keyword search filters its statistics AND its results


# ---------------- keyword statistics do not leak ----------------
def test_keyword_scores_do_not_depend_on_documents_the_person_cannot_see(corpus):
    """Rare-word weights (IDF) are computed over visible chunks only: a hidden document must not change a visible score."""
    def score():
        return {h.file_name: h.keyword_score for h in search("salary payroll", k=20, embedder=corpus, mode="keyword", principal=acme())}
    before = score()
    with connect() as conn:                                                       # make the hidden documents vanish completely
        conn.execute("UPDATE documents SET deleted_at = now() WHERE file_name <> 'pub.md'")
    assert before == score() and before["pub.md"] > 0


# ---------------- the rest of the pipeline passes the asker along ----------------
def test_retrieve_hands_the_asker_to_search(monkeypatch):
    got = {}

    def fake_search(question, **kw):
        got.update(kw)
        return []
    monkeypatch.setattr(retrieve_mod, "search", fake_search)
    who = acme("hr")
    retrieve_mod.retrieve("q", principal=who)
    assert got["principal"] is who and got["rerank"] is True


def test_answer_and_answer_stream_hand_the_asker_to_retrieve(monkeypatch):
    from indexing import answer as answer_mod
    seen_by = []

    def fake_retrieve(question, k=None, embedder=None, principal=None):
        seen_by.append(principal)
        raise RuntimeError("stop here")

    monkeypatch.setattr(answer_mod, "retrieve", fake_retrieve)
    who = acme("hr")
    with pytest.raises(RuntimeError, match="stop here"):
        answer_mod.answer("q", principal=who)
    with pytest.raises(RuntimeError, match="stop here"):
        next(answer_mod.answer_stream("q", principal=who))
    assert seen_by == [who, who]


def test_the_api_and_the_cli_default_to_the_least_privileged_asker():
    from api.main import ANONYMOUS
    assert ANONYMOUS.roles == () and ANONYMOUS.tenant == "acme"


def test_the_search_cli_asks_as_a_person_with_no_special_role_unless_told_otherwise(corpus, capsys, monkeypatch):
    monkeypatch.setattr(search_mod, "get_embedder", lambda: corpus)
    assert search_mod.main(["salary policy", "--mode", "keyword", "-k", "20"]) == 0
    out = capsys.readouterr().out
    assert "pub.md" in out and "hr.md" not in out and "admin.md" not in out
    assert search_mod.main(["salary policy", "--mode", "keyword", "-k", "20", "--roles", "hr"]) == 0
    assert "hr.md" in capsys.readouterr().out
    assert search_mod.main(["salary", "--roles", "wizard"]) == 1 and "bad asker" in capsys.readouterr().out
