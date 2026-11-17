"""
The web API.

  uvicorn api.main:app --host 127.0.0.1 --port 8000          (needs: docker compose up -d, GROQ_API_KEY in .env)
  then open http://127.0.0.1:8000/docs

  GET  /health        is everything this API needs available?
  POST /ask           {"question": "..."}  ->  the whole answer as JSON
  POST /ask/stream    same question; the answer arrives as Server-Sent Events while it is being written:
                        event: retrieval   {"answerable", "code", "sources"}
                        event: token       {"text": "..."}            (zero or more)
                        event: reset       {"reason": "..."}          a model failed half-way: clear the text so far
                        event: done        the final answer, AUTHORITATIVE (replace the streamed text with it)
                        event: error       {"message", "request_id"}  something broke after the stream began

NOT yet in place (later phases): login / roles (Phase 5), rate limits and abuse guards (Phase 6).
Until then keep it on 127.0.0.1: anyone who can reach this port can ask questions.

Privacy: the question text is never written to the logs (only its length); questions may contain personal details.
"""
import json
import time
import uuid
from contextlib import asynccontextmanager

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.schemas import AskRequest, AskResponse, HealthResponse, LoginRequest, TokenResponse
from indexing.access import Principal
from indexing import audit, auth, pii, ratelimit
from indexing.answer import answer, answer_stream, answer_to_dict
from indexing.db import can_connect
from indexing.llm import LLMAuthError
from ingestion.config import settings
from ingestion.logs import get_logger

log = get_logger()


class Unauthorized(Exception):
    """No token, or a token that is not valid. The caller is told only 'sign in again', never why."""


bearer = HTTPBearer(auto_error=False, description="The token from POST /auth/login")


def current_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    """Who is asking, from the Authorization header. Everything but /health and /auth/login depends on this."""
    if creds is None or not creds.credentials:
        raise Unauthorized()
    try:
        principal = auth.get_verifier().verify(creds.credentials)
    except auth.AuthError as e:
        log.info("auth_rejected", request_id=_rid(request), reason=str(e)[:80])           # the reason is for the log only
        raise Unauthorized() from e
    return principal


def ip_gate(request: Request) -> None:
    """First gate: how many questions this address sends, BEFORE any token is verified (a flood of bad tokens must not reach the database)."""
    ratelimit.get_limiter().ask_address(ratelimit.client_address(request))


def user_gate(principal: Principal = Depends(current_principal)) -> Principal:
    """Third gate: how many questions this person asks (a minute and a day)."""
    ratelimit.get_limiter().ask_person(principal.user_id)
    return principal


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the two local models once at startup, so the first user does not wait for them."""
    try:
        from indexing.embeddings import get_embedder
        from indexing.rerank import score_pairs
        get_embedder().embed_query("warm up")
        score_pairs("warm up", ["warm up"])
        log.info("api_ready")
    except Exception as e:                       # the API still starts; /health says what is missing
        log.warning("api_warmup_failed", error=f"{type(e).__name__}: {e}")
    yield


app = FastAPI(title="Enterprise Document Assistant", version="0.3.0", lifespan=lifespan,
              description="Ask questions about company documents. Answers come with citations; "
                          "if the documents do not contain the answer, the assistant says so.")


# ---------- request size: a body must declare its length and stay small ----------
@app.middleware("http")
async def body_limit_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            return JSONResponse(status_code=411, content={"detail": "The request must declare its size (Content-Length)."})
        if int(length) > settings.max_body_bytes:
            return JSONResponse(status_code=413, content={"detail": f"The request is too large (limit {settings.max_body_bytes} bytes)."})
    return await call_next(request)


# ---------- every request gets an id (header + logs) ----------
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


# ---------- errors: a clear message for the user, details only in the logs ----------
@app.exception_handler(LLMAuthError)
async def llm_auth_error(request: Request, exc: LLMAuthError):
    log.error("llm_auth_error", request_id=_rid(request), error=str(exc))
    return JSONResponse(status_code=503, content={"detail": "The assistant is not configured correctly. Please contact the administrator.",
                                                  "request_id": _rid(request)})


@app.exception_handler(ratelimit.RateLimited)
async def rate_limited(request: Request, exc: ratelimit.RateLimited):
    log.warning("rate_limited", request_id=_rid(request), scope=exc.scope, retry_after=exc.retry_after)     # a log line, not a database row: a flood must not become a write flood
    return JSONResponse(status_code=429, content={"detail": f"Too many requests. Please try again in {exc.retry_after} seconds.", "request_id": _rid(request)},
                        headers={"Retry-After": str(exc.retry_after)})


@app.exception_handler(ratelimit.LimiterUnavailable)
async def limiter_unavailable(request: Request, exc: ratelimit.LimiterUnavailable):
    log.error("rate_limiter_unavailable", request_id=_rid(request), error=str(exc))
    return JSONResponse(status_code=503, content={"detail": "The service is temporarily unavailable. Please try again later.", "request_id": _rid(request)},
                        headers={"Retry-After": "5"})                                              # fail closed: never "no limits"


@app.exception_handler(Unauthorized)
async def unauthorized(request: Request, exc: Unauthorized):
    return JSONResponse(status_code=401, content={"detail": "Please sign in again.", "request_id": _rid(request)}, headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(auth.AuthConfigError)
async def auth_config_error(request: Request, exc: auth.AuthConfigError):
    log.error("auth_not_configured", request_id=_rid(request), error=str(exc))
    return JSONResponse(status_code=503, content={"detail": "Sign-in is not configured correctly. Please contact the administrator.",
                                                  "request_id": _rid(request)})


@app.exception_handler(psycopg.OperationalError)
async def database_error(request: Request, exc: psycopg.OperationalError):
    log.error("database_unavailable", request_id=_rid(request), error=str(exc)[:200])
    return JSONResponse(status_code=503, content={"detail": "The document database is not available right now. Please try again later.",
                                                  "request_id": _rid(request)})


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.exception("unexpected_error", request_id=_rid(request))
    # this handler runs OUTSIDE the request-id middleware, so the header must be set here by hand
    return JSONResponse(status_code=500, content={"detail": "Something went wrong. Please try again.", "request_id": _rid(request)},
                        headers={"X-Request-ID": _rid(request)})


# ---------- audit helpers ----------
def _audit_best_effort(event: str, **kw) -> None:
    try:
        audit.record(event, **kw)
    except Exception as e:                      # an answer or a refusal that was already decided is not undone by a logging problem
        log.error("audit_write_failed", audit_event=event, error=f"{type(e).__name__}: {str(e)[:120]}")


def _audit_retrieval(rid: str, principal: Principal, question_chars: int, pii_kinds=()):
    """Called after the search and BEFORE any model sees a chunk. Not best effort: if this cannot be written, the request fails."""
    def hook(retrieval):
        audit.record("ask_retrieved", request_id=rid, principal=principal, doc_ids=[h.doc_id for h in retrieval.hits], question_chars=question_chars,
                     pii_kinds=pii_kinds)
    return hook


def _mask_question(text: str, rid: str) -> pii.Masked:
    masked = pii.mask(text)
    if masked.found:
        log.info("pii_masked", request_id=rid, kinds=masked.kinds, count=sum(masked.counts.values()))       # kinds and a count, never a value
    return masked


def _audit_finished(rid: str, principal: Principal, result: dict, ms: int) -> None:
    _audit_best_effort("ask_finished", request_id=rid, principal=principal, status=result.get("status"), model=result.get("model"),
                       chunk_ids=[c["chunk_id"] for c in result.get("citations", [])], latency_ms=ms)


# ---------- endpoints ----------
@app.get("/health", response_model=HealthResponse)
def health():
    rerank_dir = settings.models_dir / ("models--" + settings.rerank_model.replace("/", "--"))
    try:
        limiter_ok = ratelimit.get_limiter().healthy()
    except Exception:
        limiter_ok = False
    h = HealthResponse(status="ok", database=can_connect(), embedding_model=any(settings.models_dir.rglob("tokenizer.json")),
                       reranker=rerank_dir.exists(), llm_key_configured=bool(settings.groq_api_key), rate_limiter=limiter_ok)
    if not (h.database and h.embedding_model and h.reranker and h.llm_key_configured and h.rate_limiter):
        h.status = "degraded"
    return JSONResponse(status_code=200 if h.database else 503, content=h.model_dump())


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request):
    """Username + password in, short-lived signed token out. One message for every kind of failure (no hint which usernames exist)."""
    limiter = ratelimit.get_limiter()
    limiter.login_attempt(ratelimit.client_address(request))             # too many attempts from one address: 429
    limiter.login_account_open(body.username)                           # too many failures for this typed name: 429, even with the right password
    try:
        token, ttl, user = auth.login(body.username, body.password)
    except auth.AuthError:
        limiter.login_failed(body.username)
        log.info("login_failed", request_id=_rid(request))                 # never the username or password
        _audit_best_effort("login_failed", request_id=_rid(request), subject=audit.subject_hash(body.username))
        return JSONResponse(status_code=401, content={"detail": "Invalid username or password.", "request_id": _rid(request)},
                            headers={"WWW-Authenticate": "Bearer"})
    audit.record("login_ok", request_id=_rid(request), user_id=user.user_id, tenant=user.tenant)       # no record = no sign-in
    limiter.login_succeeded(body.username)
    log.info("login_ok", request_id=_rid(request), user_id=user.user_id)
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl, "request_id": _rid(request)}


@app.post("/ask", response_model=AskResponse)
def ask(body: AskRequest, request: Request, _ip: None = Depends(ip_gate), principal: Principal = Depends(user_gate)):
    start, rid = time.perf_counter(), _rid(request)
    question = _mask_question(body.question, rid)               # personal data is replaced HERE: the LLM, the logs and the audit never see it
    with ratelimit.get_limiter().slot(principal.user_id):                                 # at most a few questions in flight per person
        result = answer_to_dict(answer(question.text, principal=principal, on_retrieval=_audit_retrieval(rid, principal, len(body.question), question.kinds)))
    result["request_id"], result["masked"] = rid, question.kinds
    ms = int((time.perf_counter() - start) * 1000)
    _audit_finished(rid, principal, result, ms)
    log.info("ask", request_id=rid, user_id=principal.user_id, status=result["status"], model=result["model"], question_chars=len(body.question), ms=ms)
    return result


def sse(event: str, data: dict) -> str:
    """One Server-Sent Event: 'event: name' + 'data: json' + a blank line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/ask/stream")
def ask_stream(body: AskRequest, request: Request, _ip: None = Depends(ip_gate), principal: Principal = Depends(user_gate)):
    rid, start = _rid(request), time.perf_counter()
    question = _mask_question(body.question, rid)
    limiter = ratelimit.get_limiter()
    slot = limiter.acquire_slot(principal.user_id)             # held until the stream ends (or the person leaves)
    try:
        events = answer_stream(question.text, principal=principal, on_retrieval=_audit_retrieval(rid, principal, len(body.question), question.kinds))
        first = next(events)        # runs the search BEFORE the response starts, so a database problem is a proper 503, not a broken stream
        first = (first[0], {**first[1], "masked": question.kinds})
    except BaseException:
        limiter.release_slot(slot)
        raise

    def body_iter():
        try:
            yield sse(*first)
            for event, data in events:
                if event == "done":
                    data["request_id"], data["masked"] = rid, question.kinds
                    ms = int((time.perf_counter() - start) * 1000)
                    _audit_finished(rid, principal, data, ms)
                    log.info("ask_stream", request_id=rid, user_id=principal.user_id, status=data["status"], model=data["model"],
                             question_chars=len(body.question), ms=ms)
                yield sse(event, data)
        except LLMAuthError as e:
            log.error("llm_auth_error", request_id=rid, error=str(e))
            yield sse("error", {"message": "The assistant is not configured correctly. Please contact the administrator.", "request_id": rid})
        except Exception:
            log.exception("stream_failed", request_id=rid)
            yield sse("error", {"message": "Something went wrong. Please try again.", "request_id": rid})
        finally:
            events.close()          # the user left (or we finished): stop the model right away
            limiter.release_slot(slot)

    return StreamingResponse(body_iter(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- actions and approvals (Phase 7): imported last because it uses the gates defined above ----------
from api.actions_routes import router as actions_router  # noqa: E402

app.include_router(actions_router)
