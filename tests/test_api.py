"""Phase 3 Stage 5 tests: the FastAPI app (/health, /ask, /ask/stream). The answering itself is faked: these tests
are about the HTTP layer - validation, request ids, error handling, the SSE format and privacy."""
import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from api import main
from api.main import app, sse
from indexing.llm import LLMAuthError
from ingestion.config import settings

QUESTION = "How many sick leaves do I get? my employee id is 4711"

GOOD = {"status": "answered", "text": "You get 12 sick leaves per year [1].",
        "citations": [{"n": 1, "file_name": "leave_policy.pdf", "heading_path": "Leave Policy > Sick Leave",
                       "where": "page 1", "chunk_id": "c1"}],
        "model": "openai/gpt-oss-120b", "fell_back": False, "latency_ms": 1234, "prompt_version": "answer-v1"}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)          # real servers turn exceptions into 500s; so do we


@pytest.fixture
def logged(monkeypatch):
    calls = []

    class Recorder:
        def __getattr__(self, level):
            return lambda event, **kw: calls.append((level, event, kw))

    monkeypatch.setattr(main, "log", Recorder())
    return calls


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def fake_stream(events):
    def gen(question, *a, **k):
        yield from events
    return gen


# ---------- /health ----------
def test_health_all_good(client, monkeypatch):
    monkeypatch.setattr(main, "can_connect", lambda: True)
    monkeypatch.setattr(settings, "groq_api_key", "set")
    r = client.get("/health")
    body = r.json()
    assert r.status_code == 200 and body["database"] is True and body["llm_key_configured"] is True
    assert set(body) == {"status", "database", "embedding_model", "reranker", "llm_key_configured"}


def test_health_without_database_is_503_and_degraded(client, monkeypatch):
    monkeypatch.setattr(main, "can_connect", lambda: False)
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "degraded" and r.json()["database"] is False


def test_health_never_reveals_the_key(client, monkeypatch):
    monkeypatch.setattr(main, "can_connect", lambda: True)
    monkeypatch.setattr(settings, "groq_api_key", "gsk_super_secret_value")
    assert "gsk_super_secret_value" not in client.get("/health").text


# ---------- /ask ----------
def test_ask_returns_the_answer_with_citations(client, monkeypatch):
    monkeypatch.setattr(main, "answer", lambda q: __import__("types").SimpleNamespace())
    monkeypatch.setattr(main, "answer_to_dict", lambda a: dict(GOOD))
    r = client.post("/ask", json={"question": QUESTION})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "answered" and body["text"].startswith("You get 12")
    assert body["citations"][0]["file_name"] == "leave_policy.pdf" and body["model"] == "openai/gpt-oss-120b"
    assert body["request_id"] == r.headers["X-Request-ID"] and len(body["request_id"]) >= 8


def test_a_given_request_id_is_kept(client, monkeypatch):
    monkeypatch.setattr(main, "answer", lambda q: None)
    monkeypatch.setattr(main, "answer_to_dict", lambda a: dict(GOOD))
    r = client.post("/ask", json={"question": "x"}, headers={"X-Request-ID": "trace-me-123"})
    assert r.headers["X-Request-ID"] == "trace-me-123" and r.json()["request_id"] == "trace-me-123"


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "   \n "}, {"question": "x" * 1001},
                                     {"question": 42}, {"q": "wrong field"}])
def test_bad_questions_are_rejected_before_any_work(client, monkeypatch, payload):
    called = []
    monkeypatch.setattr(main, "answer", lambda q: called.append(q))
    r = client.post("/ask", json=payload)
    assert r.status_code == 422 and not called


def test_the_question_is_trimmed(client, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "answer", lambda q: seen.append(q))
    monkeypatch.setattr(main, "answer_to_dict", lambda a: dict(GOOD))
    client.post("/ask", json={"question": "   padded question \n"})
    assert seen == ["padded question"]


def test_wrong_method_and_unknown_path(client):
    assert client.get("/ask").status_code == 405 and client.get("/nope").status_code == 404


def test_the_question_text_is_never_logged(client, monkeypatch, logged):
    """Questions can contain personal details (an employee id, a health condition): log only the length."""
    monkeypatch.setattr(main, "answer", lambda q: None)
    monkeypatch.setattr(main, "answer_to_dict", lambda a: dict(GOOD))
    client.post("/ask", json={"question": QUESTION})
    assert logged and all(QUESTION not in json.dumps(kw, default=str) and "4711" not in json.dumps(kw, default=str)
                          for _, _, kw in logged)
    assert any(kw.get("question_chars") == len(QUESTION) for _, _, kw in logged)


# ---------- errors: clear for the user, no internals ----------
def test_missing_llm_key_is_a_503_without_details(client, monkeypatch):
    def boom(q):
        raise LLMAuthError("GROQ_API_KEY is not set (gsk_leaky_value)")
    monkeypatch.setattr(main, "answer", boom)
    r = client.post("/ask", json={"question": "x"})
    assert r.status_code == 503 and "not configured" in r.json()["detail"]
    assert "GROQ" not in r.text and "gsk_" not in r.text and r.json()["request_id"] == r.headers["X-Request-ID"]


def test_database_down_is_a_503(client, monkeypatch):
    def boom(q):
        raise psycopg.OperationalError("connection to server at 10.0.0.5 refused")
    monkeypatch.setattr(main, "answer", boom)
    r = client.post("/ask", json={"question": "x"})
    assert r.status_code == 503 and "10.0.0.5" not in r.text and "database" in r.json()["detail"]


def test_unexpected_errors_are_a_generic_500(client, monkeypatch):
    def boom(q):
        raise RuntimeError("secret internal detail /etc/passwd")
    monkeypatch.setattr(main, "answer", boom)
    r = client.post("/ask", json={"question": "x"})
    assert r.status_code == 500 and "secret" not in r.text and "passwd" not in r.text
    assert r.json()["request_id"] == r.headers["X-Request-ID"]


# ---------- /ask/stream ----------
def test_sse_format():
    assert sse("token", {"text": "héllo"}) == 'event: token\ndata: {"text": "héllo"}\n\n'


def test_stream_sends_events_in_order_with_the_request_id(client, monkeypatch):
    done = dict(GOOD)
    monkeypatch.setattr(main, "answer_stream", fake_stream([
        ("retrieval", {"answerable": True, "code": "ok", "sources": 2}),
        ("token", {"text": "You get 12 "}), ("token", {"text": "sick leaves [1]."}), ("done", done)]))
    r = client.post("/ask/stream", json={"question": QUESTION}, headers={"X-Request-ID": "abc123"})
    events = parse_sse(r.text)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["X-Request-ID"] == "abc123"
    assert [e for e, _ in events] == ["retrieval", "token", "token", "done"]
    assert events[-1][1]["request_id"] == "abc123" and events[-1][1]["status"] == "answered"
    assert "".join(d["text"] for e, d in events if e == "token") == "You get 12 sick leaves [1]."


def test_stream_validates_like_ask(client, monkeypatch):
    called = []
    monkeypatch.setattr(main, "answer_stream", lambda q: called.append(q))
    assert client.post("/ask/stream", json={"question": ""}).status_code == 422 and not called


def test_a_problem_before_the_stream_starts_is_a_proper_503_not_a_broken_stream(client, monkeypatch):
    def gen(question, *a, **k):
        raise psycopg.OperationalError("db down")
        yield
    monkeypatch.setattr(main, "answer_stream", gen)
    r = client.post("/ask/stream", json={"question": "x"})
    assert r.status_code == 503 and r.headers["content-type"].startswith("application/json")


def test_a_problem_after_the_stream_started_becomes_an_error_event(client, monkeypatch):
    closed = []

    def gen(question, *a, **k):
        try:
            yield "retrieval", {"answerable": True, "code": "ok", "sources": 1}
            yield "token", {"text": "You get "}
            raise RuntimeError("secret internal detail")
        finally:
            closed.append(True)
    monkeypatch.setattr(main, "answer_stream", gen)
    r = client.post("/ask/stream", json={"question": "x"})
    events = parse_sse(r.text)
    assert r.status_code == 200 and [e for e, _ in events] == ["retrieval", "token", "error"]
    assert "secret" not in r.text and events[-1][1]["message"] and events[-1][1]["request_id"]
    assert closed == [True]


def test_a_bad_key_during_the_stream_is_an_error_event_without_details(client, monkeypatch):
    def gen(question, *a, **k):
        yield "retrieval", {"answerable": True, "code": "ok", "sources": 1}
        raise LLMAuthError("GROQ_API_KEY rejected gsk_leaky")
    monkeypatch.setattr(main, "answer_stream", gen)
    r = client.post("/ask/stream", json={"question": "x"})
    assert "gsk_" not in r.text and "GROQ" not in r.text and parse_sse(r.text)[-1][0] == "error"


def test_the_stream_is_closed_when_it_finishes(client, monkeypatch):
    closed = []

    def gen(question, *a, **k):
        try:
            yield "retrieval", {"answerable": False, "code": "weak_match", "sources": 0}
            yield "done", dict(GOOD)
        finally:
            closed.append(True)
    monkeypatch.setattr(main, "answer_stream", gen)
    client.post("/ask/stream", json={"question": "x"})
    assert closed == [True]
