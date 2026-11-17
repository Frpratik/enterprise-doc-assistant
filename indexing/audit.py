"""
Audit log (Phase 5, Stage 4): an append-only record of who asked, which documents were put in front of the model, and how it ended.

  login_ok / login_failed   a sign-in (failed ones store only a keyed hash of the username)
  ask_retrieved             written AFTER the search and BEFORE any model sees a chunk. If this write fails the request fails:
                            no document text is shown to a model without a record of who asked for it (fail closed)
  ask_finished              how it ended: status, model, time, which chunks the answer cited (best effort: the answer was already allowed)

Never stored: the question, the answer, passwords, tokens, document text. Only ids, counts and lengths (and the KINDS of personal data a question contained).
  python -m indexing.manage audit [--user USERNAME] [--limit N]
"""
import hashlib
import hmac

from indexing.db import connect
from ingestion.config import settings

EVENTS = ("login_ok", "login_failed", "ask_retrieved", "ask_finished")


def subject_hash(username: str) -> str:
    """A keyed hash of a typed username: the same name always gives the same value (so repeated attacks show up), but the
    log does not contain what was typed and cannot be reversed by guessing names without the server's secret."""
    key = (settings.auth_secret or "no-secret").encode("utf-8")
    return hmac.new(key, (username or "").strip().lower().encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16]


def record(event: str, *, request_id: str, principal=None, user_id: str | None = None, tenant: str | None = None, subject: str | None = None,
           doc_ids=(), chunk_ids=(), status: str | None = None, model: str | None = None, question_chars: int | None = None,
           latency_ms: int | None = None, pii_kinds=()) -> None:
    if event not in EVENTS:
        raise ValueError(f"unknown audit event {event!r}")
    if principal is not None:
        user_id, tenant = user_id or principal.user_id, tenant or principal.tenant
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (request_id, event, user_id, tenant_id, subject_hash, doc_ids, chunk_ids, status, model, question_chars, latency_ms, pii_kinds) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (request_id, event, user_id, tenant, subject, sorted(set(doc_ids)), list(chunk_ids), status, model, question_chars, latency_ms, sorted(set(pii_kinds))))


def recent(limit: int = 20, user_id: str | None = None) -> list[tuple]:
    """Newest first: (at, event, user_id, tenant, status, doc_ids, chunk_ids, question_chars, latency_ms, request_id, subject_hash)."""
    with connect() as conn:
        return conn.execute(
            "SELECT to_char(at, 'YYYY-MM-DD HH24:MI:SS'), event, user_id, tenant_id, status, doc_ids, chunk_ids, question_chars, latency_ms, request_id, subject_hash "
            "FROM audit_log WHERE (%s::text IS NULL OR user_id = %s) ORDER BY audit_id DESC LIMIT %s", (user_id, user_id, int(limit))).fetchall()
