"""Phase 3 Stage 5 tests: LLM streaming (fallback, reset, cleanup), the safe streaming filter, and answer_stream.
No network and no database: the LLM is a scripted fake."""
import pytest

from indexing import answer as answer_mod
from indexing.answer import (BUSY_MESSAGE, REFUSAL_TOKEN, StreamFilter, answer, answer_stream, answer_to_dict)
from indexing.confidence import NO_ANSWER_MESSAGE, Decision
from indexing.llm import LLMAuthError, LLMClient, LLMResult, LLMUnavailable
from indexing.retrieve import Retrieval
from indexing.search import Hit
from ingestion.config import settings

MAIN, FALLBACK, SMALL = "m-main", "m-fallback", "m-small"


@pytest.fixture(autouse=True)
def models(monkeypatch):
    monkeypatch.setattr(settings, "llm_main_model", MAIN)
    monkeypatch.setattr(settings, "llm_fallback_model", FALLBACK)
    monkeypatch.setattr(settings, "llm_small_model", SMALL)


class ApiError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__("error")
        self.status_code = status
        if retry_after is not None:
            self.response = type("R", (), {"headers": {"retry-after": str(retry_after)}})()


class ScriptedStream(LLMClient):
    """script[model] = list of attempts. An attempt is an Exception (fails at once) or (pieces, error_or_None)."""

    def __init__(self, script):
        self.sleeps, self.calls, self.closed, self.keep_alive = [], [], [], []
        super().__init__(sleep=self.sleeps.append)
        self.script = {m: list(v) for m, v in script.items()}

    def _stream_call(self, model, messages, max_tokens, temperature):
        gen = self._stream(model)
        self.keep_alive.append(gen)             # hold a reference: Python must NOT be able to close it for us
        return gen

    def _stream(self, model):
        self.calls.append(model)
        attempt = self.script[model].pop(0)
        try:
            if isinstance(attempt, Exception):
                raise attempt
            pieces, error = attempt
            for p in pieces:
                yield p
            if error:
                raise error
        finally:
            self.closed.append(model)                       # runs when the stream ends OR is closed early


def run(llm, **kw):
    return list(llm.stream([{"role": "user", "content": "hi"}], **kw))


# ---------- LLM streaming ----------
def test_pieces_arrive_in_order_then_a_result():
    llm = ScriptedStream({MAIN: [(["Hel", "lo ", "there"], None)]})
    events = run(llm)
    assert [e for e in events[:-1]] == [("token", "Hel"), ("token", "lo "), ("token", "there")]
    kind, res = events[-1]
    assert kind == "done" and res.text == "Hello there" and res.model == MAIN and not res.fell_back


def test_failure_before_the_first_word_falls_back_silently():
    llm = ScriptedStream({MAIN: [ApiError(503), ApiError(503)], FALLBACK: [(["from fallback"], None)]})
    events = run(llm)
    assert [k for k, _ in events] == ["token", "done"]                     # the user never saw a reset
    assert events[-1][1].model == FALLBACK and events[-1][1].fell_back


def test_failure_half_way_sends_a_reset_then_retries():
    llm = ScriptedStream({MAIN: [(["Emp", "loyees"], ApiError(503)), (["Employees get 12 days"], None)]})
    events = run(llm)
    assert [k for k, _ in events] == ["token", "token", "reset", "token", "done"]
    assert events[-1][1].text == "Employees get 12 days" and llm.sleeps == [1.0]     # only the second attempt counts


def test_long_rate_limit_half_way_switches_model_without_waiting():
    llm = ScriptedStream({MAIN: [(["part"], ApiError(429, retry_after=900))], FALLBACK: [(["whole answer"], None)]})
    events = run(llm)
    assert [k for k, _ in events] == ["token", "reset", "token", "done"] and llm.sleeps == []
    assert events[-1][1].model == FALLBACK


def test_empty_stream_moves_to_the_next_model():
    llm = ScriptedStream({MAIN: [([], None)], FALLBACK: [(["real"], None)]})
    assert run(llm)[-1][1].text == "real"


def test_bad_key_stops_the_stream():
    llm = ScriptedStream({MAIN: [ApiError(401)], FALLBACK: [(["never"], None)]})
    with pytest.raises(LLMAuthError):
        run(llm)
    assert llm.calls == [MAIN]


def test_all_models_failing_raises():
    llm = ScriptedStream({m: [ApiError(500), ApiError(500)] for m in (MAIN, FALLBACK, SMALL)})
    with pytest.raises(LLMUnavailable):
        run(llm)


def test_closing_the_stream_early_closes_the_model_stream():
    """The user closed the page: the model must stop, and no other model may be tried."""
    llm = ScriptedStream({MAIN: [(["a", "b", "c", "d"], None)], FALLBACK: [(["never"], None)]})
    gen = llm.stream([{"role": "user", "content": "hi"}])
    assert next(gen) == ("token", "a")
    gen.close()
    assert llm.closed == [MAIN] and llm.calls == [MAIN]


# ---------- the safe streaming filter ----------
def shown(pieces):
    f = StreamFilter()
    out = "".join(f.feed(p) for p in pieces)
    return out + f.flush()


def test_plain_text_passes_through():
    assert shown(["Employees ", "get 12 ", "days [1]."]) == "Employees get 12 days [1]."


def test_a_citation_split_across_pieces_is_held_until_complete():
    f = StreamFilter()
    assert f.feed("You get 12 days [") == "You get 12 days "          # '[' might be the start of a citation
    assert f.feed("1") == ""
    assert f.feed("].") == "[1]."
    assert shown(["Two sources [1", ", 2]."]) == "Two sources [1][2]."


def test_all_citation_styles_are_rewritten_like_the_final_answer():
    assert shown(["12 days", "【1】", " and 8 more【2†L1-L3】."]) == "12 days[1] and 8 more[2]."


def test_a_refusal_is_never_shown_even_in_pieces():
    assert shown(["NOT", "_IN_", "DOCUMENTS"]) == ""
    assert shown(["Not_in_", "documents", ". The excerpts only mention salary."]) == ""
    assert shown(["  NOT_IN_DOCUMENTS"]) == ""


def test_answers_that_merely_start_like_the_token_are_not_held_back():
    assert shown(["No", ", you cannot [1]."]) == "No, you cannot [1]."
    assert shown(["Not allowed ", "[1]."]) == "Not allowed [1]."
    assert shown(["Notice period is 60 days [1]."]) == "Notice period is 60 days [1]."


def test_text_that_only_looks_like_a_citation_is_released_at_the_end():
    assert shown(["See [", "x"]) == "See [x"
    assert shown(["Use [" + "a" * 30 + " here"]) == "Use [" + "a" * 30 + " here"      # too long to be a marker


def test_reset_forgets_everything():
    f = StreamFilter()
    f.feed("half an answer [")
    f.reset()
    assert f.feed("Fresh start [1].") == "Fresh start [1]." and f.flush() == ""


# ---------- answer_stream ----------
def hit(n, content, heading="Leave Policy > Sick Leave"):
    return Hit(1.0, f"c{n}", "d1", 1, "leave_policy.pdf", heading, {"pages": [n]}, "text", content, vector_score=0.8, rerank_score=5.0)


HITS = [hit(1, "Employees get 12 sick leaves per year."), hit(2, "Employees get 8 casual leaves.", "Casual")]


class FakeLLM:
    """Implements both chat() and stream() from the same canned text, so the two paths can be compared."""

    def __init__(self, text=None, pieces=None, events=None, error=None):
        self.text, self.pieces, self.events, self.error = text, pieces, events, error
        self.streams_started = self.streams_closed = 0

    def chat(self, messages, tier="main", **kw):
        if self.error:
            raise self.error
        return LLMResult(self.text, MAIN, tier, [], {}, 50)

    def stream(self, messages, tier="main", **kw):
        self.streams_started += 1
        try:
            if self.error:
                raise self.error
            for e in self.events or [("token", p) for p in (self.pieces or [self.text])]:
                yield e
            yield "done", LLMResult(self.text, MAIN, tier, [], {}, 50)
        finally:
            self.streams_closed += 1


@pytest.fixture
def retrieval(monkeypatch):
    def set_(answerable=True):
        d = Decision(answerable, "ok" if answerable else "weak_match", "test", 5.0, 0.8)
        monkeypatch.setattr(answer_mod, "retrieve", lambda q, k=None, embedder=None: Retrieval(q, HITS, d))
    return set_


def collect(gen):
    return list(gen)


def test_stream_event_order_for_a_good_answer(retrieval):
    retrieval()
    llm = FakeLLM("You get 12 sick leaves per year [1].", pieces=["You get 12 ", "sick leaves ", "per year [", "1]."])
    events = collect(answer_stream("sick days?", llm=llm))
    assert [e for e, _ in events] == ["retrieval", "token", "token", "token", "token", "done"]
    assert events[0][1] == {"answerable": True, "code": "ok", "sources": 2}
    streamed = "".join(d["text"] for e, d in events if e == "token")
    done = events[-1][1]
    assert streamed == done["text"] == "You get 12 sick leaves per year [1]."
    assert done["status"] == "answered" and done["citations"][0]["file_name"] == "leave_policy.pdf" and done["model"] == MAIN
    assert "raw" not in done and "hits" not in done                       # internals stay inside


def test_gate_refusal_streams_nothing_and_never_calls_the_llm(retrieval):
    retrieval(answerable=False)
    llm = FakeLLM("unused")
    events = collect(answer_stream("capital of France?", llm=llm))
    assert [e for e, _ in events] == ["retrieval", "done"] and events[1][1]["status"] == "refused_gate"
    assert events[1][1]["text"] == NO_ANSWER_MESSAGE and llm.streams_started == 0


def test_llm_refusal_never_shows_a_single_token(retrieval):
    retrieval()
    events = collect(answer_stream("joining bonus?", llm=FakeLLM(REFUSAL_TOKEN, pieces=["NOT", "_IN_", "DOCUMENTS"])))
    assert [e for e, _ in events] == ["retrieval", "done"]
    assert events[-1][1]["status"] == "refused_llm" and events[-1][1]["text"] == NO_ANSWER_MESSAGE


def test_uncited_answer_is_streamed_but_the_final_event_replaces_it(retrieval):
    """The honest trade-off of streaming: words appear before the checks can run. 'done' is authoritative."""
    retrieval()
    events = collect(answer_stream("sick days?", llm=FakeLLM("You get 12 days.", pieces=["You get ", "12 days."])))
    assert "".join(d["text"] for e, d in events if e == "token") == "You get 12 days."
    assert events[-1][0] == "done" and events[-1][1]["status"] == "uncited" and events[-1][1]["text"] == NO_ANSWER_MESSAGE


def test_reset_clears_the_filter_and_only_the_retry_counts(retrieval):
    retrieval()
    evs = [("token", "Half an ans"), ("reset", "m-main failed while answering"), ("token", "12 sick leaves [1].")]
    events = collect(answer_stream("sick days?", llm=FakeLLM("12 sick leaves [1].", events=evs)))
    assert [e for e, _ in events] == ["retrieval", "token", "reset", "token", "done"]
    assert events[-1][1]["text"] == "12 sick leaves [1]."


def test_all_models_failing_ends_with_a_busy_message(retrieval):
    retrieval()
    events = collect(answer_stream("sick days?", llm=FakeLLM(error=LLMUnavailable([]))))
    assert events[-1][1]["status"] == "llm_unavailable" and events[-1][1]["text"] == BUSY_MESSAGE


def test_a_bad_key_is_raised_for_the_api_to_handle(retrieval):
    retrieval()
    with pytest.raises(LLMAuthError):
        collect(answer_stream("sick days?", llm=FakeLLM(error=LLMAuthError("no key"))))


def test_closing_answer_stream_stops_the_llm_stream(retrieval):
    retrieval()
    llm = FakeLLM("a b c", pieces=["a ", "b ", "c"])
    gen = answer_stream("q", llm=llm)
    assert next(gen)[0] == "retrieval" and next(gen)[0] == "token"
    gen.close()
    assert llm.streams_started == 1 and llm.streams_closed == 1


@pytest.mark.parametrize("text", [
    "You get 12 sick leaves [1].", "NOT_IN_DOCUMENTS", "NOT_IN_DOCUMENTS - only salary is mentioned",
    "No citation at all.", "Made up source [9].", "Both [1][2] are real.", "12 days【1】 and 8 days【2】.",
    "Real [1] plus made up [7].", "Sources [1, 2]."])
def test_stream_and_normal_answer_agree_on_the_final_result(retrieval, text):
    """The two paths share one set of checks, so what the user finally sees must be identical."""
    retrieval()
    normal = answer_to_dict(answer("q", llm=FakeLLM(text)))
    streamed = collect(answer_stream("q", llm=FakeLLM(text, pieces=[text[:7], text[7:]])))[-1][1]
    for key in ("status", "text", "citations", "model"):
        assert streamed[key] == normal[key], key
