"""Phase 5 Stage 4 tests: /ask and /ask/stream need a signed-in person; the person flows into retrieval; every request is audited."""
import json
import re
import time

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient

from api import main
from api.main import app
from indexing import answer as answer_mod, audit, auth, manage as manage_mod, rerank as rerank_mod, search as search_mod, sync, users
from indexing.answer import NO_ANSWER_MESSAGE
from indexing.db import connect
from indexing.embeddings import Embedder
from indexing.llm import LLMResult
from ingestion.config import settings

SECRET = "api-secret-" + "x" * 40
PASSWORD = "a long enough password"
QUESTION = "what is the salary policy payroll bonus rule"
DOCS = {"pub.md": ("acme", ["all"], "CANARYPUB"), "hr.md": ("acme", ["hr"], "CANARYHR"), "admin.md": ("acme", ["admin"], "CANARYADMIN"),
        "globex.md": ("globex", ["all"], "CANARYGLOBEX")}


class FakeEmbedder(Embedder):
    name, dims = "fake-model", 26

    def embed_documents(self, texts):
        out = []
        for t in texts:
            v = np.array([t.lower().count(c) for c in "abcdefghijklmnopqrstuvwxyz"], dtype=np.float32) + 0.01
            out.append(v / np.linalg.norm(v))
        return out


class RecordingLLM:
    """Writes 'first line of excerpt [1]' and cites it, unless the question names a CANARY code that is not in the excerpts (then: not found)."""
    def __init__(self):
        self.calls = []

    def _text(self, messages):
        content = messages[1]["content"]
        excerpts = content.split("<excerpts>\n", 1)[1].split("\n</excerpts>", 1)[0]
        asked = re.findall(r"CANARY\w+", content.replace(excerpts, ""))
        if any(a not in excerpts for a in asked):
            return "NOT_IN_DOCUMENTS"
        return "The rule is in the first excerpt [1]."

    def chat(self, messages, tier="main", **kw):
        self.calls.append(messages)
        return LLMResult(self._text(messages), "fake-llm", tier, [], {}, 5)

    def stream(self, messages, tier="main", **kw):
        self.calls.append(messages)
        text = self._text(messages)
        for word in text.split(" "):
            yield "token", word + " "
        yield "done", LLMResult(text, "fake-llm", tier, [], {}, 5)

    @property
    def prompts(self):
        return "\n".join(m["content"] for call in self.calls for m in call)


@pytest.fixture
def world(db_url, temp_data, tmp_path, monkeypatch):
    """Four documents in two companies, four people, a recording LLM. No model is downloaded: embeddings and reranking are faked."""
    monkeypatch.setattr(settings, "auth_secret", SECRET)
    auth.set_verifier(None)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [{"match": n, "tenant": t, "roles": r} for n, (t, r, _) in DOCS.items()]}), encoding="utf-8")
    monkeypatch.setattr(settings, "access_rules_path", rules)
    src = temp_data / "source"
    src.mkdir()
    for name, (_, _, canary) in DOCS.items():
        (src / name).write_text(f"# {name}\n\n## Pay\nThe salary policy and payroll bonus rules. Reference {canary}.\n", encoding="utf-8")
    emb = FakeEmbedder()
    sync.run_sync(src, embedder=emb)
    monkeypatch.setattr(search_mod, "get_embedder", lambda: emb)
    monkeypatch.setattr(rerank_mod, "score_pairs", lambda q, texts: [5.0] * len(texts))
    llm = RecordingLLM()
    monkeypatch.setattr(answer_mod, "default_client", lambda: llm)
    users.create_user("anna", "acme", "", PASSWORD)
    users.create_user("hana", "acme", "hr", PASSWORD)
    users.create_user("adam", "acme", "admin", PASSWORD)
    users.create_user("greg", "globex", "", PASSWORD)
    yield TestClient(app, raise_server_exceptions=False), llm
    auth.set_verifier(None)


def token_of(client, name):
    r = client.post("/auth/login", json={"username": name, "password": PASSWORD})
    assert r.status_code == 200
    return r.json()["access_token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def ask(client, token, question=QUESTION, path="/ask"):
    return client.post(path, json={"question": question}, headers=bearer(token))


def sse_events(text):
    return [(b.splitlines()[0][7:], json.loads(b.splitlines()[1][6:])) for b in text.strip().split("\n\n")]


def audit_rows(event=None):
    with connect() as conn:
        rows = conn.execute("SELECT event, user_id, tenant_id, subject_hash, doc_ids, chunk_ids, status, question_chars, request_id FROM audit_log ORDER BY audit_id").fetchall()
    return [r for r in rows if event is None or r[0] == event]


def doc_ids(*names):
    with connect() as conn:
        return {n: conn.execute("SELECT doc_id FROM documents WHERE file_name = %s", (n,)).fetchone()[0] for n in names}


# ---------------- no token, no answer ----------------
def bad_tokens(world_client):
    expired, _ = auth.issue_token(users.get_user(doc_user_id("anna")), now=time.time() - 7200)
    forged = token_of(world_client, "anna")[:-3] + "abc"
    return {"no header": {}, "basic scheme": {"Authorization": "Basic YW5uYTpwdw=="}, "empty bearer": {"Authorization": "Bearer "},
            "garbage": bearer("not-a-token"), "expired": bearer(expired), "forged signature": bearer(forged),
            "token in query only": {}}


def doc_user_id(name):
    with connect() as conn:
        return conn.execute("SELECT user_id FROM users WHERE username = %s", (name,)).fetchone()[0]


@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_without_a_valid_token_the_answer_is_a_401_and_no_model_is_called(world, path):
    client, llm = world
    bodies = set()
    for name, headers in bad_tokens(client).items():
        url = path + ("?access_token=" + token_of(client, "hana") if name == "token in query only" else "")
        r = client.post(url, json={"question": QUESTION}, headers=headers)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer", name
        assert r.headers["content-type"].startswith("application/json"), name           # a proper HTTP error, never a half-open event stream
        body = r.json()
        assert body.pop("request_id")
        bodies.add(json.dumps(body))
    assert bodies == {json.dumps({"detail": "Please sign in again."})}                 # the same words whatever was wrong: forged, expired, missing
    assert llm.calls == [] and audit_rows("ask_retrieved") == []                        # nothing was searched, nothing reached a model


def test_a_disabled_or_deleted_person_is_a_401_at_once(world):
    client, llm = world
    token = token_of(client, "hana")
    assert ask(client, token).status_code == 200
    users.set_active("hana", False)
    assert ask(client, token).status_code == 401 and ask(client, token, path="/ask/stream").status_code == 401
    with connect() as conn:
        conn.execute("DELETE FROM users WHERE username = 'hana'")
    assert ask(client, token).status_code == 401


def test_health_and_login_stay_public(world):
    client, _ = world
    assert client.get("/health").status_code in (200, 503)
    assert client.post("/auth/login", json={"username": "anna", "password": PASSWORD}).status_code == 200


def test_without_an_auth_secret_the_endpoints_answer_503_not_200(world, monkeypatch):
    client, llm = world
    token = token_of(client, "anna")
    monkeypatch.setattr(settings, "auth_secret", "")
    r = ask(client, token)
    assert r.status_code == 503 and "secret" not in r.text.lower().replace("not configured", "") and llm.calls == []


# ---------------- the person decides what is searched ----------------
@pytest.mark.parametrize("who, allowed", [("anna", {"CANARYPUB"}), ("hana", {"CANARYPUB", "CANARYHR"}), ("adam", {"CANARYPUB", "CANARYADMIN"}),
                                          ("greg", {"CANARYGLOBEX"})])
@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_a_model_and_a_person_only_ever_see_what_that_person_may_read(world, who, allowed, path):
    client, llm = world
    r = ask(client, token_of(client, who), path=path)
    assert r.status_code == 200 and len(llm.calls) == 1
    everything = {d[2] for d in DOCS.values()}
    shown = r.text                                                    # the whole response: answer, citations, every stream event
    for canary in everything:
        assert (canary in llm.prompts) == (canary in allowed), (who, canary, "prompt")
        assert canary not in shown, (who, canary, "response")           # the fake answer never repeats a code, so any code would be a leak
    if path == "/ask":
        names = {c["file_name"] for c in r.json()["citations"]}
        assert names <= {n for n, d in DOCS.items() if d[2] in allowed}


def test_asking_for_a_forbidden_document_looks_exactly_like_asking_for_a_missing_one(world):
    client, llm = world
    token = token_of(client, "anna")

    def reply(q):
        body = ask(client, token, q).json()
        body.pop("request_id"), body.pop("latency_ms")
        return body
    forbidden, missing = reply("what does CANARYHR say"), reply("what does CANARYNOSUCHTHING say")
    assert forbidden == missing and forbidden["text"] == NO_ANSWER_MESSAGE and forbidden["citations"] == []
    assert "CANARYHR" not in llm.prompts.split("what does CANARYNOSUCHTHING")[0].replace("what does CANARYHR say", "")
    allowed = ask(client, token_of(client, "hana"), "what does CANARYHR say").json()
    assert allowed["status"] == "answered"                           # the same question works for a person who may read it


def test_the_streaming_events_never_mention_a_forbidden_document(world):
    client, _ = world
    r = ask(client, token_of(client, "anna"), "what does CANARYADMIN say", path="/ask/stream")
    events = sse_events(r.text)
    assert events[0][0] == "retrieval" and events[-1][0] == "done" and events[-1][1]["text"] == NO_ANSWER_MESSAGE
    assert "CANARYADMIN" not in r.text and "admin.md" not in r.text


def test_a_role_change_applies_to_the_very_next_question(world):
    client, llm = world
    token = token_of(client, "anna")
    ask(client, token)
    assert "CANARYHR" not in llm.prompts
    users.set_roles("anna", "hr")
    ask(client, token)                                               # the SAME token: roles are read from the table, not the token
    assert "CANARYHR" in llm.calls[-1][1]["content"]
    users.set_roles("anna", "")
    ask(client, token)
    assert "CANARYHR" not in llm.calls[-1][1]["content"]


def test_a_validly_signed_token_for_the_wrong_company_gets_nothing(world):
    client, llm = world
    claims = {**__import__("jwt").decode(token_of(client, "anna"), options={"verify_signature": False}), "tenant": "globex"}
    forged = __import__("jwt").encode(claims, SECRET, algorithm="HS256")           # we pretend the attacker knew the secret
    assert ask(client, forged).status_code == 401 and llm.calls == []


# ---------------- the audit log ----------------
def test_every_question_leaves_a_record_of_who_and_which_documents_but_not_what_was_asked(world):
    client, llm = world
    ids = doc_ids("pub.md", "hr.md", "admin.md", "globex.md")
    question = "tell me about the salary policy payroll bonus rule of SECRETPROJECT"
    ask(client, token_of(client, "hana"), question)
    ask(client, token_of(client, "hana"), question, path="/ask/stream")
    ask(client, token_of(client, "anna"), question)
    rows = audit_rows()
    retrieved = [r for r in rows if r[0] == "ask_retrieved"]
    assert len(retrieved) == 3 and len([r for r in rows if r[0] == "ask_finished"]) == 3
    hana_id, anna_id = doc_user_id("hana"), doc_user_id("anna")
    assert [r[1] for r in retrieved] == [hana_id, hana_id, anna_id] and {r[2] for r in retrieved} == {"acme"}
    assert set(retrieved[0][4]) == {ids["pub.md"], ids["hr.md"]} and set(retrieved[2][4]) == {ids["pub.md"]}      # exactly what each person's model saw
    assert all(r[7] == len(question) for r in retrieved)
    finished = [r for r in rows if r[0] == "ask_finished"]
    assert all(r[6] == "answered" and r[5] for r in finished[:2]) and all(r[8] for r in rows)
    dump = json.dumps([[str(x) for x in r] for r in rows])
    for secret in (question, "SECRETPROJECT", "salary policy", "CANARY", PASSWORD, SECRET):
        assert secret not in dump                                    # no question, no answer, no document text, no secrets


def test_the_cited_chunks_in_the_audit_belong_to_documents_the_person_may_read(world):
    client, _ = world
    ids = doc_ids("pub.md", "hr.md", "admin.md")
    ask(client, token_of(client, "anna"))
    cited = [c for r in audit_rows("ask_finished") for c in r[5]]
    with connect() as conn:
        docs = {conn.execute("SELECT doc_id FROM chunks WHERE chunk_id = %s", (c,)).fetchone()[0] for c in cited}
    assert cited and docs == {ids["pub.md"]}


def test_logins_are_audited_and_failed_logins_store_only_a_keyed_hash(world):
    client, _ = world
    token_of(client, "anna")
    client.post("/auth/login", json={"username": "hana", "password": "wrong password!!"})
    client.post("/auth/login", json={"username": "nobody-here", "password": "wrong password!!"})
    client.post("/auth/login", json={"username": "Nobody-Here", "password": "wrong password again"})
    ok, failed = audit_rows("login_ok"), audit_rows("login_failed")
    assert [r[1] for r in ok] == [doc_user_id("anna")] and ok[0][2] == "acme"
    assert len(failed) == 3 and all(r[1] is None and r[3] for r in failed)
    assert failed[1][3] == failed[2][3] == audit.subject_hash("nobody-here") and failed[0][3] != failed[1][3]          # same account, same value
    dump = json.dumps([[str(x) for x in r] for r in audit_rows()])
    assert "nobody-here" not in dump.lower() and "wrong password" not in dump


def test_the_audit_log_is_append_only(world):
    client, _ = world
    ask(client, token_of(client, "anna"))
    for sql in ("UPDATE audit_log SET user_id = 'someone-else'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with connect() as conn:
                conn.execute(sql)
    assert len(audit_rows()) >= 3


def test_audit_event_names_are_checked(world):
    with pytest.raises(ValueError):
        audit.record("ask_whatever", request_id="r")
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as conn:
            conn.execute("INSERT INTO audit_log (request_id, event) VALUES ('r', 'made_up')")


@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_if_the_audit_record_cannot_be_written_no_model_sees_anything(world, monkeypatch, path):
    client, llm = world
    token = token_of(client, "hana")
    real = audit.record

    def failing(event, **kw):
        if event == "ask_retrieved":
            raise psycopg.errors.DiskFull("simulated")
        return real(event, **kw)
    monkeypatch.setattr(audit, "record", failing)
    r = ask(client, token, path=path)
    assert r.status_code in (500, 503) and "CANARY" not in r.text and llm.calls == []          # a database problem is a 503, anything else a 500: never a 200


def test_a_login_without_an_audit_record_gives_no_token(world, monkeypatch):
    client, _ = world
    monkeypatch.setattr(audit, "record", lambda *a, **k: (_ for _ in ()).throw(psycopg.errors.DiskFull("simulated")))
    r = client.post("/auth/login", json={"username": "anna", "password": PASSWORD})
    assert r.status_code in (500, 503) and "access_token" not in r.text


def test_a_problem_writing_the_final_record_does_not_undo_an_answer_already_allowed(world, monkeypatch):
    client, _ = world
    token = token_of(client, "hana")
    real = audit.record
    monkeypatch.setattr(audit, "record", lambda event, **kw: (_ for _ in ()).throw(psycopg.errors.DiskFull("x")) if event == "ask_finished" else real(event, **kw))
    assert ask(client, token).status_code == 200
    assert len(audit_rows("ask_retrieved")) == 1 and audit_rows("ask_finished") == []


def test_the_logs_name_the_person_by_id_and_never_by_question(world):
    client, _ = world
    ask(client, token_of(client, "hana"), "a question with SECRETPROJECT inside")
    logs = settings.log_path.read_text(encoding="utf-8")
    assert doc_user_id("hana") in logs and "SECRETPROJECT" not in logs and PASSWORD not in logs and "Bearer" not in logs


# ---------------- the audit command ----------------
def test_the_audit_command(world, capsys):
    client, _ = world
    ask(client, token_of(client, "hana"), "a question with SECRETPROJECT inside")
    client.post("/auth/login", json={"username": "ghost", "password": "wrong password!!"})
    assert manage_mod.main(["audit"]) == 0
    out = capsys.readouterr().out
    assert "ask_retrieved" in out and "ask_finished" in out and "login_failed" in out and "login_ok" in out and "name#" in out
    assert "SECRETPROJECT" not in out and "ghost" not in out
    assert manage_mod.main(["audit", "--user", "hana"]) == 0
    out = capsys.readouterr().out
    assert "ask_retrieved" in out and "login_failed" not in out
    assert manage_mod.main(["audit", "--user", "nobody"]) == 1 and "no user" in capsys.readouterr().out
