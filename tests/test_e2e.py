"""
Phase 3 Stage 6: end-to-end through the REAL pipeline - only the LLM is a script.

  real documents -> real chunks -> real embeddings + keyword index in a throwaway database
  -> real search + reranker + "I don't know" gate -> the FastAPI app -> a SCRIPTED LLM

So these tests need the two local models but no API key and no network. They check what the unit tests
cannot: that the right excerpts really reach the LLM, and that questions that must be refused never reach it.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

from api.main import app
from indexing import answer as answer_mod
from indexing import search_eval
from indexing.llm import LLMResult
from indexing.retrieval_eval import norm
from ingestion.config import PROJECT_ROOT, settings

needs_models = pytest.mark.skipif(
    not (settings.models_dir / ("models--" + settings.rerank_model.replace("/", "--"))).exists()
    or not any(settings.models_dir.rglob("tokenizer.json")), reason="local models not downloaded")
pytestmark = needs_models


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


ANSWERABLE = [(q["q"], q["answers"]) for p in search_eval.SETS.values() for q in load(p)["questions"]]
UNANSWERABLE = load(PROJECT_ROOT / "evals" / "unanswerable.json")


@pytest.fixture(scope="module")
def corpus(samples, pg_server_url):
    """The 12 sample documents, indexed once for this whole file, in a throwaway database."""
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "database_url", pg_server_url)
    docs = list(dict.fromkeys(d for p in search_eval.SETS.values() for d in load(p)["documents"]))
    with search_eval.temp_database():
        search_eval.index_golden_documents(docs)
        yield
    mp.undo()


# ---------- a scripted LLM ----------
def excerpt_blocks(messages) -> list[tuple[str, str]]:
    """What the LLM was really given: [(file name, excerpt text), ...] in prompt order."""
    user = messages[1]["content"]
    body = user.split("<excerpts>\n", 1)[1].split("\n</excerpts>", 1)[0]
    out = []
    for block in re.split(r"\n\n(?=\[\d+\] )", body):
        header, _, text = block.partition("\n")
        out.append((re.match(r"\[\d+\] ([^,|]+)", header).group(1).strip(), text))
    return out


class ScriptedLLM:
    """mode 'extract': quote the first sentence of excerpt [1] and cite it | 'refuse' | 'uncited'."""

    def __init__(self, mode="extract"):
        self.mode, self.calls = mode, []

    def _text(self, messages):
        if self.mode == "refuse":
            return "NOT_IN_DOCUMENTS"
        first = excerpt_blocks(messages)[0][1].strip().splitlines()[-1][:80]
        return first if self.mode == "uncited" else f"{first} [1]"

    def chat(self, messages, tier="main", **kw):
        self.calls.append(messages)
        return LLMResult(self._text(messages), "scripted", tier, [], {}, 5)

    def stream(self, messages, tier="main", **kw):
        self.calls.append(messages)
        text = self._text(messages)
        for word in text.split(" "):
            yield "token", word + " "
        yield "done", LLMResult(text, "scripted", tier, [], {}, 5)


@pytest.fixture
def llm(monkeypatch):
    fake = ScriptedLLM()
    monkeypatch.setattr(answer_mod, "default_client", lambda: fake)    # answer() and answer_stream() get their client here
    return fake


@pytest.fixture
def api(corpus, monkeypatch):
    # This file measures retrieval through the real API for all 44 questions, including salary and headcount documents that are
    # restricted. Until login exists (Stage 3-4) the API asker is anonymous (no roles), which correctly cannot see them: so here the API asks
    # as an auditor. Access itself is tested in test_access_filter.py.
    from api import main
    from indexing.access import Principal
    monkeypatch.setattr(main, "ANONYMOUS", Principal.auditor("acme"))
    return TestClient(app, raise_server_exceptions=False)


def parse_sse(text):
    return [(b.splitlines()[0][7:], json.loads(b.splitlines()[1][6:])) for b in text.strip().split("\n\n")]


# ---------- the right excerpts reach the LLM ----------
def test_every_answerable_question_gets_its_answer_in_front_of_the_llm(api, llm):
    """44 golden questions through POST /ask: the excerpt that holds the answer must be among those sent to the LLM."""
    missing = []
    for question, answers in ANSWERABLE:
        before = len(llm.calls)
        r = api.post("/ask", json={"question": question})
        assert r.status_code == 200, question
        if len(llm.calls) == before:                                  # the gate refused it
            missing.append(f"GATE BLOCKED: {question}")
            continue
        blocks = excerpt_blocks(llm.calls[-1])
        if not any(f == file_name and norm(phrase) in norm(text) for file_name, phrase in answers for f, text in blocks):
            missing.append(f"ANSWER NOT SENT: {question}")
    assert not missing, missing                                       # all 44: retrieval does not depend on the LLM, so no slack is needed
    assert len(llm.calls) == len(ANSWERABLE)


def test_a_normal_question_returns_an_answer_with_the_right_source(api, llm):
    r = api.post("/ask", json={"question": "How many days notice must I give before resigning?"})
    body = r.json()
    assert body["status"] == "answered" and "60 days" in body["text"] and body["model"] == "scripted"
    assert body["citations"][0]["file_name"] == "leave_policy.pdf" and body["citations"][0]["where"]
    assert body["request_id"] == r.headers["X-Request-ID"]
    assert len(llm.calls) == 1


# ---------- what must NOT reach the LLM ----------
def test_off_topic_questions_never_reach_the_llm(api, llm):
    for question in UNANSWERABLE["off_topic"]:
        body = api.post("/ask", json={"question": question}).json()
        assert body["status"] == "refused_gate" and "couldn't find" in body["text"] and not body["citations"], question
    assert llm.calls == []                                           # not one LLM call for 10 off-topic questions


def test_the_llm_can_still_refuse_a_question_that_passed_the_gate(api, llm):
    llm.mode = "refuse"
    body = api.post("/ask", json={"question": "How many days of leave do I get for my marriage?"}).json()
    assert body["status"] == "refused_llm" and "couldn't find" in body["text"] and body["citations"] == []
    assert len(llm.calls) == 1                                       # near-topic: it passed the score gate, the LLM refused


def test_an_answer_without_a_citation_is_replaced_by_the_not_found_message(api, llm):
    llm.mode = "uncited"
    body = api.post("/ask", json={"question": "How long is maternity leave?"}).json()
    assert body["status"] == "uncited" and "couldn't find" in body["text"] and "26 weeks" not in body["text"]


# ---------- streaming, for real ----------
def test_streaming_end_to_end(api, llm):
    r = api.post("/ask/stream", json={"question": "What is the Helpdesk extension number? ext 4455"})
    events = parse_sse(r.text)
    names = [e for e, _ in events]
    assert names[0] == "retrieval" and names[-1] == "done" and "token" in names and "error" not in names
    assert events[0][1]["answerable"] is True and events[0][1]["sources"] >= 1
    done = events[-1][1]
    streamed = "".join(d["text"] for e, d in events if e == "token").strip()
    assert done["status"] == "answered" and streamed == done["text"] and "4455" in done["text"]
    assert done["citations"][0]["file_name"] == "employee_handbook.docx" and done["request_id"] == r.headers["X-Request-ID"]


def test_a_refused_stream_ends_right_after_the_search(api, llm):
    r = api.post("/ask/stream", json={"question": "What is the capital of France?"})
    events = parse_sse(r.text)
    assert [e for e, _ in events] == ["retrieval", "done"] and events[1][1]["status"] == "refused_gate"
    assert llm.calls == []


# ---------- the whole thing is healthy ----------
def test_health_sees_the_real_database_and_models(api):
    body = api.get("/health").json()
    assert body["database"] is True and body["embedding_model"] is True and body["reranker"] is True
