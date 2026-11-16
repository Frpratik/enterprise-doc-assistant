"""Phase 5 Stage 1 tests: access labels (tenant + roles) on documents. Fail closed: no label = invisible."""
import json
import os
import uuid

import numpy as np
import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from indexing import access, migrate as migrate_mod, sync
from indexing.access import (ALL, ROLES, AccessError, Label, Rule, RulesFile, is_catch_all, label_columns, load_rules, match_rule, parse_roles,
                             validate_tenant)
from indexing.db import connect
from indexing.embeddings import Embedder
from indexing.manage import main as manage
from ingestion.config import PROJECT_ROOT, settings


# ---------------- pure: roles, tenants, labels ----------------
def test_roles_are_normalised_sorted_and_deduplicated():
    assert parse_roles("hr, Finance ,hr") == ("finance", "hr")
    assert parse_roles(["MANAGER", "it"]) == ("it", "manager")
    assert parse_roles("all") == (ALL,)


@pytest.mark.parametrize("bad, message", [("hr_", "unknown role"), ("", "at least one role"), ([], "at least one role"), ("   ,  ", "at least one role"),
                                          ("all,hr", "cannot be combined"), (42, "must be a list"), (["hr", "superuser"], "superuser")])
def test_a_typo_in_a_role_is_an_error_not_a_hidden_document(bad, message):
    with pytest.raises(AccessError, match=message):
        parse_roles(bad)


def test_the_error_for_an_unknown_role_lists_the_known_ones():
    with pytest.raises(AccessError) as e:
        parse_roles("hr_")
    assert all(r in str(e.value) for r in ROLES)


@pytest.mark.parametrize("tenant", ["acme", "globex", "my-company_2", "ab"])
def test_valid_tenants(tenant):
    assert validate_tenant(tenant) == tenant


@pytest.mark.parametrize("tenant", ["", "A", "Acme", "acme corp", "1acme", "a", "x" * 40, "unassigned", None, 5])
def test_invalid_tenants_are_rejected(tenant):
    with pytest.raises(AccessError):
        validate_tenant(tenant)


def test_a_label_validates_itself_and_describes_itself():
    label = Label("acme", ("hr", "finance"), "rule")
    assert label.roles == ("finance", "hr") and label.describe() == "acme: finance, hr"
    assert Label("acme", (ALL,), "manual").describe() == "acme: everyone"
    for bad in (lambda: Label("Acme", ("hr",), "rule"), lambda: Label("acme", ("nope",), "rule"), lambda: Label("acme", ("hr",), "none"),
                lambda: Label("acme", (), "rule")):
        with pytest.raises(AccessError):
            bad()
    with pytest.raises(Exception):
        label.tenant = "other"                                              # frozen: a label cannot be changed after the fact


def test_label_columns_for_the_database():
    assert label_columns(Label("acme", ("hr",), "rule")) == ("acme", ["hr"], "labelled", "rule")
    assert label_columns(None) == ("unassigned", [], "unlabelled", "none")


# ---------------- pure: the rules file ----------------
def write_rules(path, rules, version=1):
    path.write_text(json.dumps({"version": version, "rules": rules}), encoding="utf-8")
    return path


def test_rules_are_loaded_validated_and_lowercased(tmp_path):
    p = write_rules(tmp_path / "r.json", [{"match": "Salary_*.XLSX", "tenant": "acme", "roles": ["HR", "finance"], "why": "pay"}])
    assert load_rules(p) == [Rule("salary_*.xlsx", "acme", ("finance", "hr"), "pay")]


def test_a_missing_rules_file_means_no_rules_so_everything_stays_hidden(tmp_path):
    assert load_rules(tmp_path / "nothing.json") == []


@pytest.mark.parametrize("rules, message", [
    ([{"match": "a.md", "tenant": "acme", "roles": ["hr_"]}], r"rule 1 \('a.md'\): unknown role"),
    ([{"match": "a.md", "tenant": "Acme", "roles": ["hr"]}], "invalid tenant"),
    ([{"match": "a.md", "tenant": "acme", "roles": []}], "at least one role"),
    ([{"match": "a.md", "tenant": "acme"}], "at least one role"),
    ([{"tenant": "acme", "roles": ["hr"]}], "needs a 'match' pattern"),
    ([{"match": "  ", "tenant": "acme", "roles": ["hr"]}], "needs a 'match' pattern"),
    (["a.md"], "needs a 'match' pattern"),
    ([{"match": "a.md", "tenant": "acme", "roles": ["hr"]}, {"match": "A.MD", "tenant": "acme", "roles": ["all"]}], "appears twice"),
])
def test_invalid_rules_stop_loading_with_a_message_that_names_the_rule(tmp_path, rules, message):
    with pytest.raises(AccessError, match=message):
        load_rules(write_rules(tmp_path / "r.json", rules))


def test_a_broken_rules_file_is_an_error(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AccessError, match="not valid JSON"):
        load_rules(tmp_path / "bad.json")
    with pytest.raises(AccessError, match="must look like"):
        load_rules(write_rules(tmp_path / "v2.json", [], version=2))


def test_the_first_matching_rule_wins_and_matching_ignores_case():
    rules = [Rule("globex_salary_*", "globex", ("hr",)), Rule("globex_*", "globex", (ALL,)), Rule("*.md", "acme", ("hr",))]
    assert match_rule("Globex_Salary_Bands.MD", rules).roles == ("hr",)
    assert match_rule("globex_leave.md", rules).roles == (ALL,)
    assert match_rule("notes.md", rules).tenant == "acme"
    assert match_rule("notes.txt", rules) is None                               # no rule = unlabelled
    assert match_rule("a.md", []) is None


def test_catch_all_patterns_are_recognised():
    assert is_catch_all("*") and is_catch_all("**") and is_catch_all("*?") and not is_catch_all("*.md") and not is_catch_all("a*")


def test_the_rules_file_is_reloaded_when_it_changes(tmp_path, monkeypatch):
    path = write_rules(tmp_path / "r.json", [{"match": "a.md", "tenant": "acme", "roles": ["hr"]}])
    monkeypatch.setattr(settings, "access_rules_path", path)
    rf = RulesFile()
    assert rf.get()[0].roles == ("hr",) and rf.get() is rf.get()                # cached while the file is unchanged
    write_rules(path, [{"match": "a.md", "tenant": "acme", "roles": ["all"]}])
    os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns + 10_000_000))
    assert rf.get()[0].roles == (ALL,)


# ---------------- the committed rules and the Phase 5 sample corpus ----------------
def test_the_committed_rules_are_valid_and_have_no_catch_all():
    rules = load_rules(PROJECT_ROOT / "access_rules.json")
    assert len(rules) >= 20 and not [r.pattern for r in rules if is_catch_all(r.pattern)]


def test_every_document_of_the_answer_exam_has_a_rule_and_the_restricted_ones_are_right():
    from indexing.answer_golden import load_golden
    rules = load_rules(PROJECT_ROOT / "access_rules.json")
    golden = load_golden()
    by_name = {d: match_rule(d, rules) for d in golden.documents}
    assert all(by_name.values()), [d for d, r in by_name.items() if r is None]
    restricted = {d: r.roles for d, r in by_name.items() if r.roles != (ALL,)}
    assert restricted == {"salary_structure.xlsx": ("finance", "hr"), "salary_revision_matrix.xlsx": ("finance", "hr", "manager"),
                          "mixed_quarterly_report.pdf": ("hr", "manager"), "vendor_onboarding_sop.docx": ("finance", "procurement")}
    assert {r.tenant for r in by_name.values()} == {"acme"}                     # the answer exam is a single-company exam


def test_the_second_company_and_the_confidential_documents_are_labelled():
    rules = load_rules(PROJECT_ROOT / "access_rules.json")
    assert (match_rule("globex_leave_policy.md", rules).tenant, match_rule("globex_leave_policy.md", rules).roles) == ("globex", (ALL,))
    assert (match_rule("globex_salary_bands.md", rules).tenant, match_rule("globex_salary_bands.md", rules).roles) == ("globex", ("hr",))
    assert match_rule("acme_board_minutes.md", rules).roles == ("admin",) and match_rule("acme_executive_compensation.md", rules).roles == ("admin",)


def test_canaries_exist_in_exactly_one_document(samples):
    import subprocess
    import sys
    from indexing.retrieval_eval import SAMPLES
    folder = SAMPLES.parent / "access"
    if not folder.exists():
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "make_access_corpus.py")], check=True)
    seen = {}
    for p in [*sorted(folder.glob("*")), *sorted(SAMPLES.glob("*.md")), *sorted(SAMPLES.glob("*.txt")), *sorted((SAMPLES.parent / "eval2").glob("*.md"))]:
        for word in p.read_text(encoding="utf-8", errors="ignore").split():
            if word.startswith("CANARY-"):
                seen.setdefault(word.strip(".,"), []).append(p.name)
    assert sorted(seen) == ["CANARY-BOARD-7731", "CANARY-EXEC-2290", "CANARY-GLOBEX-4102", "CANARY-GLOBEX-HR-5518"]
    assert all(len(v) == 1 for v in seen.values())                              # a canary is in ONE document only


# ---------------- the database (fresh database per test) ----------------
class FakeEmbedder(Embedder):
    name, dims = "fake-model", 26

    def embed_documents(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


@pytest.fixture
def folder(db_url, temp_data):
    src = temp_data / "source"
    src.mkdir()
    return src


@pytest.fixture
def rules_file(tmp_path, monkeypatch):
    path = write_rules(tmp_path / "rules.json", [{"match": "hr_note.md", "tenant": "acme", "roles": ["hr", "finance"]},
                                                 {"match": "policy.md", "tenant": "acme", "roles": ["all"]}])
    monkeypatch.setattr(settings, "access_rules_path", path)
    return path


def put(folder, name, text):
    (folder / name).write_text(f"# {name}\n\n## Section\n{text}\n", encoding="utf-8")


def labels():
    with connect() as conn:
        return {r[0]: r[1:] for r in conn.execute("SELECT file_name || ' v' || version, tenant_id, allowed_roles, access_status, access_source "
                                                    "FROM documents ORDER BY file_name, version")}


def test_the_migration_adds_the_label_columns_and_drops_the_unused_chunk_columns(db_url):
    with connect() as conn:
        cols = lambda t: {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (t,))}
        assert {"tenant_id", "allowed_roles", "access_status", "access_source", "access_updated_at"} <= cols("documents")
        assert not ({"tenant_id", "allowed_roles"} & cols("chunks"))              # one source of truth: the document


def test_a_new_row_is_unlabelled_by_default_fail_closed(db_url):
    with connect() as conn:
        conn.execute("INSERT INTO documents (doc_id, version, file_name, hash, status) VALUES ('d', 1, 'x.md', 'h', 'done')")
        row = conn.execute("SELECT tenant_id, allowed_roles, access_status, access_source FROM documents").fetchone()
    assert row == ("unassigned", [], "unlabelled", "none")


@pytest.mark.parametrize("values", [
    "'labelled', 'acme', '{}', 'rule'",                        # labelled but nobody may see it: ambiguous
    "'labelled', 'unassigned', '{hr}', 'rule'",                # labelled but no company
    "'labelled', 'acme', '{hr}', 'none'",                      # labelled but nobody decided it
    "'unlabelled', 'acme', '{hr}', 'none'",                    # roles on an unlabelled document
    "'unlabelled', 'unassigned', '{}', 'rule'",
    "'maybe', 'acme', '{hr}', 'rule'",
])
def test_the_database_itself_refuses_a_half_labelled_document(db_url, values):
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute("INSERT INTO documents (doc_id, version, file_name, hash, status, access_status, tenant_id, allowed_roles, access_source) "
                         f"VALUES ('d', 1, 'x.md', 'h', 'done', {values})")


def test_existing_documents_become_invisible_when_the_migration_runs(pg_server_url, monkeypatch):
    """The data migration: a database from before Phase 5 has documents with no labels. After migration 004 they are UNLABELLED."""
    name = f"eda_mig_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(pg_server_url, dbname=name)
    try:
        real = migrate_mod._files()
        with monkeypatch.context() as mp:
            mp.setattr(migrate_mod, "_files", lambda: real[:3])
            migrate_mod.migrate(url, quiet=True)                                  # the database as it was before Phase 5
        with psycopg.connect(url) as conn:
            conn.execute("INSERT INTO documents (doc_id, version, file_name, hash, status, is_active) VALUES ('old', 1, 'old.pdf', 'h', 'done', true)")
            conn.execute("INSERT INTO chunks (chunk_id, doc_id, version, chunk_index, chunk_type, content, embed_text, embed_hash, token_count, is_active) "
                         "VALUES ('c1', 'old', 1, 0, 'text', 'secret salary', 'secret salary', 'e', 3, true)")
        assert migrate_mod.migrate(url, quiet=True)[0] == "004_access_labels.sql"      # later migrations may follow
        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT tenant_id, allowed_roles, access_status, access_source FROM documents").fetchone() == ("unassigned", [], "unlabelled", "none")
            assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1          # nothing deleted: only hidden
    finally:
        with psycopg.connect(pg_server_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_documents_get_their_label_from_the_rules_and_unmatched_ones_stay_hidden(folder, rules_file):
    put(folder, "hr_note.md", "Increments are decided in April.")
    put(folder, "policy.md", "Employees get 12 sick leaves.")
    put(folder, "stray.md", "Nobody wrote a rule for this file.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    assert labels() == {"hr_note.md v1": ("acme", ["finance", "hr"], "labelled", "rule"), "policy.md v1": ("acme", ["all"], "labelled", "rule"),
                        "stray.md v1": ("unassigned", [], "unlabelled", "none")}


def test_a_new_version_is_labelled_again_by_the_current_rules(folder, rules_file):
    put(folder, "policy.md", "Version one.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    write_rules(rules_file, [{"match": "policy.md", "tenant": "acme", "roles": ["hr"]}])
    os.utime(rules_file, ns=(os.stat(rules_file).st_atime_ns, os.stat(rules_file).st_mtime_ns + 10_000_000))
    put(folder, "policy.md", "Version two, edited.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    assert labels() == {"policy.md v1": ("acme", ["all"], "labelled", "rule"), "policy.md v2": ("acme", ["hr"], "labelled", "rule")}


def test_a_label_set_by_hand_survives_new_versions_and_beats_the_rules(folder, rules_file):
    put(folder, "policy.md", "Version one.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    doc_id = next(iter(connect().__enter__().execute("SELECT doc_id FROM documents").fetchone()))
    access.set_access(doc_id, "acme", "hr,finance")
    put(folder, "policy.md", "Version two, edited.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    assert labels() == {"policy.md v1": ("acme", ["finance", "hr"], "labelled", "manual"), "policy.md v2": ("acme", ["finance", "hr"], "labelled", "manual")}


def test_set_access_labels_every_version_and_can_hide_a_document_at_once(folder, rules_file):
    put(folder, "policy.md", "Version one.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    put(folder, "policy.md", "Version two, edited.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    doc_id = connect().__enter__().execute("SELECT doc_id FROM documents LIMIT 1").fetchone()[0]
    r = access.set_access(doc_id, "acme", "manager")
    assert r["versions"] == 2 and r["label"] == "acme: manager"
    hidden = access.set_access(doc_id, "acme", "", unlabel=True)
    assert hidden["versions"] == 2 and "UNLABELLED" in hidden["label"]
    assert {v[2] for v in labels().values()} == {"unlabelled"}


@pytest.mark.parametrize("args, message", [(("d", "acme", "hr_"), "unknown role"), (("d", "Acme", "hr"), "invalid tenant"), (("d", "acme", "all,hr"), "cannot be combined"),
                                           (("nope", "acme", "hr"), "no document nope")])
def test_set_access_refuses_bad_input_and_changes_nothing(folder, rules_file, args, message):
    put(folder, "policy.md", "Text.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    before = labels()
    doc_id = connect().__enter__().execute("SELECT doc_id FROM documents").fetchone()[0]
    with pytest.raises(AccessError, match=message):
        access.set_access(doc_id if args[0] == "d" else args[0], *args[1:])
    assert labels() == before


def test_changing_a_label_never_re_reads_or_re_embeds_anything(folder, rules_file):
    put(folder, "policy.md", "Employees get 12 sick leaves per year.")
    fake = FakeEmbedder()
    sync.run_sync(folder, embedder=fake)

    def snapshot():
        with connect() as conn:
            return (conn.execute("SELECT count(*) FROM embeddings").fetchone()[0],
                    conn.execute("SELECT chunk_id, embed_hash, is_active, content FROM chunks ORDER BY chunk_id").fetchall(),
                    conn.execute("SELECT indexed_at, chunk_count FROM documents").fetchone())
    before = snapshot()
    doc_id = connect().__enter__().execute("SELECT doc_id FROM documents").fetchone()[0]
    access.set_access(doc_id, "acme", "hr")
    assert snapshot() == before


def test_a_label_change_is_all_or_nothing(folder, rules_file):
    put(folder, "policy.md", "Version one.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    put(folder, "policy.md", "Version two, edited.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    before = labels()
    doc_id = connect().__enter__().execute("SELECT doc_id FROM documents LIMIT 1").fetchone()[0]
    with pytest.raises(RuntimeError):
        with connect() as conn:
            access.set_label(conn, doc_id, Label("acme", ("hr",), "manual"))
            raise RuntimeError("something failed half-way")
    assert labels() == before                                                   # rolled back: no version was left with a new label


def test_apply_rules_follows_the_rules_but_never_overrides_a_person(folder, rules_file):
    for name in ("hr_note.md", "policy.md", "stray.md", "manual.md"):
        put(folder, name, f"Text of {name}.")
    write_rules(rules_file, [{"match": "hr_note.md", "tenant": "acme", "roles": ["hr"]}, {"match": "policy.md", "tenant": "acme", "roles": ["all"]},
                             {"match": "manual.md", "tenant": "acme", "roles": ["all"]}])
    sync.run_sync(folder, embedder=FakeEmbedder())
    with connect() as conn:
        manual_id = conn.execute("SELECT doc_id FROM documents WHERE file_name = 'manual.md'").fetchone()[0]
    access.set_access(manual_id, "acme", "finance")                             # a person decided: the rules must not touch it
    write_rules(rules_file, [{"match": "hr_note.md", "tenant": "acme", "roles": ["hr", "manager"]},       # changed
                             {"match": "stray.md", "tenant": "acme", "roles": ["it"]},                    # newly covered
                             {"match": "manual.md", "tenant": "acme", "roles": ["all"]}])                 # policy.md has no rule any more
    counts = access.apply_rules()
    assert counts == {"labelled": 1, "relabelled": 1, "hidden": 1, "unchanged": 0, "kept_manual": 1}
    got = labels()
    assert got["hr_note.md v1"][:3] == ("acme", ["hr", "manager"], "labelled") and got["stray.md v1"][:3] == ("acme", ["it"], "labelled")
    assert got["policy.md v1"] == ("unassigned", [], "unlabelled", "none")      # its rule was removed: hidden, not left public
    assert got["manual.md v1"] == ("acme", ["finance"], "labelled", "manual")
    assert access.apply_rules() == {"labelled": 0, "relabelled": 0, "hidden": 0, "unchanged": 3, "kept_manual": 1}      # idempotent


def test_apply_rules_with_an_invalid_file_changes_nothing(folder, rules_file):
    put(folder, "policy.md", "Text.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    before = labels()
    write_rules(rules_file, [{"match": "policy.md", "tenant": "acme", "roles": ["hr_"]}])
    with pytest.raises(AccessError, match="unknown role"):
        access.apply_rules()
    assert labels() == before


def test_deleted_documents_are_not_reported_as_unlabelled(folder, rules_file):
    put(folder, "stray.md", "Text.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    assert [n for _, n in access.unlabelled_documents()] == ["stray.md"]
    (folder / "stray.md").unlink()
    sync.run_sync(folder, embedder=FakeEmbedder())
    assert access.unlabelled_documents() == []


# ---------------- the commands ----------------
def run_manage(capsys, *argv):
    code = manage(list(argv))
    return code, capsys.readouterr().out


def test_the_sync_command_tells_you_which_documents_nobody_can_see(folder, rules_file, capsys):
    put(folder, "policy.md", "Text.")
    put(folder, "stray.md", "Text.")
    code, out = run_manage(capsys, "sync", "--folder", str(folder))
    assert code == 0 and "NO ACCESS LABEL: 1 document(s) are invisible to everyone" in out and "stray.md" in out and "apply-rules" in out
    put(folder, "extra_policy.md", "x")
    write_rules(rules_file, [{"match": "*.md", "tenant": "acme", "roles": ["all"]}])
    run_manage(capsys, "apply-rules")
    code, out = run_manage(capsys, "sync", "--folder", str(folder))
    assert "NO ACCESS LABEL" not in out


def test_the_access_command_lists_who_can_see_what(folder, rules_file, capsys):
    put(folder, "hr_note.md", "Text.")
    put(folder, "stray.md", "Text.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    code, out = run_manage(capsys, "access")
    assert code == 0 and "rules: 2 in rules.json" in out and "finance, hr" in out and "UNLABELLED - nobody" in out
    assert "2 documents, 1 labelled, 1 unlabelled (invisible to everyone)" in out and "WARNING" not in out


def test_the_access_command_warns_about_a_catch_all_and_a_missing_or_broken_rules_file(tmp_path, folder, rules_file, capsys, monkeypatch):
    write_rules(rules_file, [{"match": "*", "tenant": "acme", "roles": ["all"]}])
    assert "WARNING: rule '*' matches EVERY file" in run_manage(capsys, "access")[1]
    monkeypatch.setattr(settings, "access_rules_path", tmp_path / "missing.json")
    assert "FILE MISSING: every document stays invisible" in run_manage(capsys, "access")[1]
    bad = write_rules(tmp_path / "bad.json", [{"match": "a", "tenant": "acme", "roles": ["x"]}])
    monkeypatch.setattr(settings, "access_rules_path", bad)
    code, out = run_manage(capsys, "access")
    assert code == 1 and "INVALID" in out and "unknown role" in out


def test_the_set_access_and_apply_rules_commands(folder, rules_file, capsys):
    put(folder, "stray.md", "Text.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    doc_id = connect().__enter__().execute("SELECT doc_id FROM documents").fetchone()[0]
    code, out = run_manage(capsys, "set-access", doc_id, "--roles", "hr,finance")
    assert code == 0 and "acme: finance, hr" in out and "no re-indexing needed" in out
    code, out = run_manage(capsys, "set-access", doc_id, "--roles", "hr_")
    assert code == 1 and "not changed" in out and "unknown role" in out
    code, out = run_manage(capsys, "set-access", doc_id, "--unlabel")
    assert code == 0 and "UNLABELLED (hidden from everyone)" in out
    with pytest.raises(SystemExit):
        manage(["set-access", doc_id])                                          # neither --roles nor --unlabel: a usage error
    run_manage(capsys, "set-access", doc_id, "--roles", "it")                   # by hand: apply-rules leaves it alone
    code, out = run_manage(capsys, "apply-rules")
    assert code == 0 and "kept (set by hand) 1" in out


def test_the_list_command_shows_the_access_label(folder, rules_file, capsys):
    put(folder, "hr_note.md", "Text.")
    put(folder, "stray.md", "Text.")
    sync.run_sync(folder, embedder=FakeEmbedder())
    out = run_manage(capsys, "list")[1]
    assert "ACCESS" in out and "acme: finance,hr" in out and "UNLABELLED" in out
