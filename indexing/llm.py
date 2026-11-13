"""
The LLM layer: one place that talks to Groq, with retries and a FALLBACK CHAIN.

  chat(messages, tier="main")   ->  LLMResult(text, model, attempts, ...)

Models (set in .env / config):
  main      gpt-oss-120b   writes the answers
  fallback  qwen3.8-27b    a different model family: if one model has a problem, the other probably doesn't
  small     gpt-oss-20b    simple, cheap jobs; also the LAST fallback

Chains:   tier "main"  : main -> fallback -> small
          tier "small" : small -> fallback

What counts as a problem, and what we do:
  rate limit (429)            wait a few seconds and retry ONCE; if the wait would be long, go to the next model at once
  timeout / connection / 5xx  retry once, then the next model
  model removed (404 / 400)   the next model at once (retrying cannot help)
  empty answer                the next model (reasoning models can use all their tokens "thinking")
  bad key (401 / 403)         STOP: every model would fail the same way; say so clearly
  every model failed          raise LLMUnavailable (the caller shows "busy, try again", it never invents an answer)

Every call records which model answered and every attempt, so Phase 8 tracing can show it.
"""
import time
from dataclasses import dataclass, field

from ingestion.config import settings
from ingestion.logs import get_logger


class LLMError(Exception):
    pass


class LLMAuthError(LLMError):
    """The API key is missing or rejected. Retrying or switching models cannot fix this."""


class LLMUnavailable(LLMError):
    """Every model in the chain failed."""

    def __init__(self, attempts: list["Attempt"]):
        self.attempts = attempts
        super().__init__("all models failed: " + "; ".join(f"{a.model}={a.outcome}" for a in attempts))


@dataclass
class Attempt:
    model: str
    outcome: str            # ok / rate_limited / temporary / model_unavailable / bad_request / empty
    ms: int = 0
    detail: str = ""


@dataclass
class LLMResult:
    text: str
    model: str
    tier: str
    attempts: list[Attempt] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def fell_back(self) -> bool:
        """True when the model that answered was not the first one tried."""
        return len(self.attempts) > 1 and self.attempts[0].model != self.model


def chain_for(tier: str) -> list[str]:
    chains = {"main": [settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model],
              "small": [settings.llm_small_model, settings.llm_fallback_model]}
    if tier not in chains:
        raise ValueError(f"unknown tier '{tier}' (use main or small)")
    return list(dict.fromkeys(chains[tier]))                      # no model twice


def classify(exc: Exception) -> tuple[str, float | None]:
    """(kind, seconds the server asked us to wait). kind: auth / rate_limited / temporary / model_unavailable / bad_request"""
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "auth", None
    if status == 429:
        retry_after = None
        try:
            retry_after = float(exc.response.headers.get("retry-after"))
        except Exception:
            pass
        return "rate_limited", retry_after
    if status == 404:
        return "model_unavailable", None
    if status == 400 and "model" in str(exc).lower():
        return "model_unavailable", None
    if status is not None and (status == 408 or status >= 500):
        return "temporary", None
    if status is None and isinstance(exc, (TimeoutError, ConnectionError)):
        return "temporary", None
    if status is None and type(exc).__name__ in ("APITimeoutError", "APIConnectionError"):
        return "temporary", None
    return "bad_request", None


def _params(model: str) -> dict:
    # gpt-oss models "think" before answering; low effort keeps answers fast and leaves tokens for the answer
    return {"reasoning_effort": "low"} if "gpt-oss" in model else {}


class LLMClient:
    def __init__(self, client=None, sleep=time.sleep, max_wait: float | None = None, retries: int = 1):
        self._client = client
        self._sleep = sleep
        self.max_wait = settings.llm_max_wait_sec if max_wait is None else max_wait
        self.retries = retries

    def _get_client(self):
        if self._client is None:
            if not settings.groq_api_key:
                raise LLMAuthError("GROQ_API_KEY is not set. Put it in .env:  GROQ_API_KEY=...")
            from groq import Groq
            self._client = Groq(api_key=settings.groq_api_key, timeout=settings.llm_timeout_sec, max_retries=0)
        return self._client

    # ----- the places that really call Groq (tests and the eval cache replace these) -----
    def _stream_call(self, model: str, messages: list[dict], max_tokens: int, temperature: float):
        """Yields the answer text piece by piece. Reasoning models also stream their 'thinking': we skip it."""
        stream = self._get_client().chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                            max_completion_tokens=max_tokens, stream=True, **_params(model))
        try:
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        finally:
            close = getattr(stream, "close", None)            # the user left: stop the model, stop paying for tokens
            if close:
                close()

    def _call(self, model: str, messages: list[dict], max_tokens: int, temperature: float) -> tuple[str, dict]:
        r = self._get_client().chat.completions.create(model=model, messages=messages, temperature=temperature,
                                                       max_completion_tokens=max_tokens, **_params(model))
        usage = {"prompt_tokens": r.usage.prompt_tokens, "completion_tokens": r.usage.completion_tokens} if r.usage else {}
        return (r.choices[0].message.content or "").strip(), usage

    def chat(self, messages: list[dict], tier: str = "main", max_tokens: int | None = None,
             temperature: float = 0.0) -> LLMResult:
        log = get_logger()
        max_tokens = max_tokens or settings.llm_answer_max_tokens
        attempts: list[Attempt] = []
        start = time.perf_counter()
        for model in chain_for(tier):
            for attempt_no in range(1 + self.retries):
                t0 = time.perf_counter()
                try:
                    text, usage = self._call(model, messages, max_tokens, temperature)
                except LLMAuthError:
                    raise
                except Exception as exc:                                  # classify, then retry / next model / stop
                    kind, retry_after = classify(exc)
                    ms = int((time.perf_counter() - t0) * 1000)
                    attempts.append(Attempt(model, kind, ms, f"{type(exc).__name__}: {str(exc)[:150]}"))
                    log.warning("llm_attempt_failed", model=model, kind=kind, attempt=attempt_no + 1)
                    if kind == "auth":
                        raise LLMAuthError(f"Groq rejected the API key ({type(exc).__name__}). Check GROQ_API_KEY in .env.") from exc
                    if kind in ("rate_limited", "temporary") and attempt_no < self.retries:
                        wait = retry_after if retry_after is not None else 1.0
                        if wait <= self.max_wait:                         # short wait: try the same model again
                            self._sleep(wait)
                            continue
                    break                                                 # long wait / not retryable: next model
                ms = int((time.perf_counter() - t0) * 1000)
                if not text:
                    attempts.append(Attempt(model, "empty", ms, "no text came back (all tokens used for thinking?)"))
                    break                                                 # next model
                attempts.append(Attempt(model, "ok", ms))
                log.info("llm_ok", model=model, tier=tier, ms=ms, fell_back=len(attempts) > 1, **usage)
                return LLMResult(text, model, tier, attempts, usage, int((time.perf_counter() - start) * 1000))
        raise LLMUnavailable(attempts)

    def stream(self, messages: list[dict], tier: str = "main", max_tokens: int | None = None,
               temperature: float = 0.0):
        """
        Same retries and fallback chain as chat(), but the answer arrives piece by piece. Yields:
          ("token", text)    a piece of the answer
          ("reset", reason)  the model failed HALF-WAY: throw away everything shown so far, a new attempt starts
          ("done", LLMResult)
        A failure BEFORE the first piece is silent (we just move on, like chat()); only a failure after the user
        already saw words needs a reset. If the consumer stops listening (the user closed the page), the model
        stream is closed right away.
        """
        log = get_logger()
        max_tokens = max_tokens or settings.llm_answer_max_tokens
        attempts: list[Attempt] = []
        start = time.perf_counter()
        for model in chain_for(tier):
            for attempt_no in range(1 + self.retries):
                t0 = time.perf_counter()
                parts: list[str] = []
                source = self._stream_call(model, messages, max_tokens, temperature)
                try:
                    for piece in source:
                        parts.append(piece)
                        yield "token", piece
                except LLMAuthError:
                    raise
                except Exception as exc:
                    kind, retry_after = classify(exc)
                    ms = int((time.perf_counter() - t0) * 1000)
                    attempts.append(Attempt(model, kind, ms, f"{type(exc).__name__}: {str(exc)[:150]}"))
                    log.warning("llm_stream_failed", model=model, kind=kind, after_pieces=len(parts))
                    if kind == "auth":
                        raise LLMAuthError(f"Groq rejected the API key ({type(exc).__name__}). Check GROQ_API_KEY in .env.") from exc
                    if parts:
                        yield "reset", f"{model} failed while answering"
                    if kind in ("rate_limited", "temporary") and attempt_no < self.retries:
                        wait = retry_after if retry_after is not None else 1.0
                        if wait <= self.max_wait:
                            self._sleep(wait)
                            continue
                    break
                finally:
                    source.close()
                text = "".join(parts).strip()
                ms = int((time.perf_counter() - t0) * 1000)
                if not text:
                    attempts.append(Attempt(model, "empty", ms, "no text came back (all tokens used for thinking?)"))
                    break
                attempts.append(Attempt(model, "ok", ms))
                log.info("llm_stream_ok", model=model, tier=tier, ms=ms, fell_back=len(attempts) > 1)
                yield "done", LLMResult(text, model, tier, attempts, {}, int((time.perf_counter() - start) * 1000))
                return
        raise LLMUnavailable(attempts)
