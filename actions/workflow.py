"""
The approval flow (Phase 7, Stage 2): from a proposal to something that really happens, with people in the loop.

  1. PROPOSE   submit(draft)               the assistant (Stage 3) or the person proposes; a valid draft is stored as a PENDING ACTION. Nothing happens yet.
  2. CONFIRM   confirm(action_id, fp)      the REQUESTER confirms, in a separate request, sending the fingerprint of the draft they were shown.
                                           A ticket is created. A leave request is created and waits for a manager.
  3. DECIDE    decide_leave(...)           a MANAGER (role from the database) approves or rejects a leave request. Never the requester.
  Also: cancel_action(), cancel_leave(), expire_stale().

What makes it safe (every point is tested):
  * WHAT RUNS IS WHAT WAS STORED. The confirming request carries only the fingerprint; the arguments come from the stored row, which is
    re-checked against its fingerprint (and against the rules again, because time has passed) before anything is created.
  * ONCE. The row is locked, the action moves awaiting -> executed in the same transaction as the creation, and the created row carries a UNIQUE
    action_id: a double click, a retry or two parallel requests create exactly one thing. Confirming an executed action returns the same result.
  * ONE PERSON AT A TIME. A per-person advisory lock serialises confirmations, so two different requests that would overlap or overspend the
    balance cannot both pass the check.
  * NO SELF-APPROVAL, NO CROSS-COMPANY. The decider must hold the manager role, be in the same company and be a different person.
  * EXPIRY. An unconfirmed action expires (default 30 minutes); a confirmation that arrives late is refused.
  * AUDIT IN THE SAME TRANSACTION. Every change writes its audit row in the same transaction: if the audit row cannot be written, nothing happens.
  * Someone else's action looks exactly like one that does not exist.
"""
import hashlib
import json
import secrets
from datetime import datetime

from actions import domain
from actions.domain import Draft
from indexing import audit
from indexing.access import Principal
from indexing.db import connect
from ingestion.config import settings

ACTION_COLUMNS = "action_id, kind, summary, args, fingerprint, status, created_at, expires_at, result_ref, failure"


# ---------------- errors ----------------
class ActionError(Exception):
    code = "error"

    def __init__(self, message: str, problems: list | None = None):
        super().__init__(message)
        self.problems = problems or []


class NotFound(ActionError):
    code = "not_found"


class Conflict(ActionError):
    code = "conflict"


class Expired(ActionError):
    code = "expired"


class FingerprintMismatch(ActionError):
    code = "fingerprint_mismatch"


class NotAllowed(ActionError):
    code = "not_allowed"


class Invalid(ActionError):
    code = "invalid"


# ---------------- helpers ----------------
def _action(r) -> dict:
    return {"action_id": r[0], "kind": r[1], "summary": r[2], "args": r[3], "fingerprint": r[4], "status": r[5],
            "created_at": r[6].isoformat(timespec="seconds"), "expires_at": r[7].isoformat(timespec="seconds"), "result_ref": r[8], "failure": r[9]}


def _lock_person(conn, user_id: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user_id,))


def _result(conn, a: dict) -> dict:
    """The object an executed action created."""
    if not a["result_ref"]:
        return {}
    if a["kind"] == "ticket":
        row = conn.execute(f"SELECT {domain.TICKET_COLUMNS} FROM tickets WHERE ticket_id = %s", (a["result_ref"],)).fetchone()
        return {"ticket": domain._ticket_row(row)} if row else {}
    row = conn.execute("SELECT request_id, leave_type, start_date, end_date, days, reason, status FROM leave_requests WHERE request_id = %s", (a["result_ref"],)).fetchone()
    return {"leave_request": domain._leave_row(row)} if row else {}


# ---------------- 1. propose ----------------
def expire_stale(user_id: str | None = None, request_id: str = "-") -> int:
    """Actions nobody confirmed in time become 'expired' (with an audit row each)."""
    with connect() as conn:
        rows = conn.execute("SELECT action_id, user_id, tenant_id FROM pending_actions WHERE status = 'awaiting_requester' AND expires_at <= now() "
                            "AND (%s::text IS NULL OR user_id = %s) FOR UPDATE SKIP LOCKED", (user_id, user_id)).fetchall()
        for action_id, uid, tenant in rows:
            conn.execute("UPDATE pending_actions SET status = 'expired' WHERE action_id = %s", (action_id,))
            audit.record_in(conn, "action_expired", request_id=request_id, user_id=uid, tenant=tenant, ref=action_id)
    return len(rows)


def submit(principal: Principal, draft: Draft, request_id: str = "-") -> dict:
    """Store a VALID draft as a pending action for this person. The same draft submitted again returns the pending action it already has."""
    if not draft.valid:
        raise Invalid("the draft cannot be created", draft.problems)
    if domain.fingerprint(draft.kind, draft.args, principal) != draft.fingerprint:
        raise Invalid("this draft was made for someone else or was changed")
    expire_stale(principal.user_id, request_id)
    with connect() as conn:
        _lock_person(conn, principal.user_id)
        row = conn.execute(f"SELECT {ACTION_COLUMNS} FROM pending_actions WHERE user_id = %s AND fingerprint = %s AND status = 'awaiting_requester'",
                           (principal.user_id, draft.fingerprint)).fetchone()
        if row:
            return _action(row)
        waiting = conn.execute("SELECT count(*) FROM pending_actions WHERE user_id = %s AND status = 'awaiting_requester'", (principal.user_id,)).fetchone()[0]
        if waiting >= settings.max_pending_actions:
            raise Conflict(f"{waiting} actions are already waiting for your confirmation: confirm or cancel some first")
        action_id = "ACT-" + secrets.token_hex(8)
        row = conn.execute(f"INSERT INTO pending_actions (action_id, kind, user_id, tenant_id, args, fingerprint, summary, expires_at) "
                           f"VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s, now() + make_interval(mins => %s)) RETURNING {ACTION_COLUMNS}",
                           (action_id, draft.kind, principal.user_id, principal.tenant, json.dumps(draft.args), draft.fingerprint, draft.summary,
                            int(settings.action_confirm_minutes))).fetchone()
        audit.record_in(conn, "action_proposed", request_id=request_id, principal=principal, ref=action_id, status=draft.kind)
    return _action(row)


# ---------------- own actions ----------------
def list_actions(principal: Principal, status: str | None = None, limit: int = 50) -> list[dict]:
    expire_stale(principal.user_id)
    with connect() as conn:
        rows = conn.execute(f"SELECT {ACTION_COLUMNS} FROM pending_actions WHERE user_id = %s AND (%s::text IS NULL OR status = %s) "
                            "ORDER BY created_at DESC, action_id LIMIT %s", (principal.user_id, status, status, min(int(limit), 50))).fetchall()
    return [_action(r) for r in rows]


def get_action(principal: Principal, action_id: str) -> dict:
    expire_stale(principal.user_id)
    with connect() as conn:
        row = conn.execute(f"SELECT {ACTION_COLUMNS} FROM pending_actions WHERE action_id = %s AND user_id = %s AND tenant_id = %s",
                           (str(action_id)[:40], principal.user_id, principal.tenant)).fetchone()
        if row is None:
            raise NotFound("no such action")
        a = _action(row)
        a["result"] = _result(conn, a)
    return a


# ---------------- 2. confirm ----------------
def confirm(principal: Principal, action_id: str, fingerprint: str, request_id: str = "-") -> dict:
    """The requester confirms the draft they were shown. Returns {"action", "result", "already_executed"}."""
    later = None                                          # raised AFTER the transaction commits (a raise inside would roll the state change back)
    with connect() as conn:
        _lock_person(conn, principal.user_id)
        row = conn.execute(f"SELECT {ACTION_COLUMNS} FROM pending_actions WHERE action_id = %s AND user_id = %s AND tenant_id = %s FOR UPDATE",
                           (str(action_id)[:40], principal.user_id, principal.tenant)).fetchone()
        if row is None:
            raise NotFound("no such action")
        a = _action(row)
        if a["status"] == "executed":                     # a retry or a double click: the same result, nothing created twice
            if fingerprint != a["fingerprint"]:
                raise FingerprintMismatch("the draft you confirmed is not the one that was stored")
            return {"action": a, "result": _result(conn, a), "already_executed": True}
        if a["status"] != "awaiting_requester":
            raise Conflict(f"this action is {a['status']}")
        if conn.execute("SELECT expires_at <= now() FROM pending_actions WHERE action_id = %s", (action_id,)).fetchone()[0]:
            conn.execute("UPDATE pending_actions SET status = 'expired' WHERE action_id = %s", (action_id,))
            audit.record_in(conn, "action_expired", request_id=request_id, principal=principal, ref=action_id)
            later = Expired("the confirmation window has passed: propose it again")
        elif fingerprint != a["fingerprint"] or domain.fingerprint(a["kind"], a["args"], principal) != a["fingerprint"]:
            raise FingerprintMismatch("the draft you confirmed is not the one that was stored")
        else:
            args = a["args"]
            if a["kind"] == "leave_request":                # time has passed: check the rules AGAIN, on this connection, under the person's lock
                draft = domain.propose_leave(principal, args["leave_type"], args["start_date"], args["end_date"], args["reason"], conn=conn)
            else:
                draft = domain.propose_ticket(principal, args["category"], args["title"], args["description"], args["priority"])
            if not draft.valid or draft.args != args:
                why = draft.problems or ["the draft is no longer what it was when it was proposed"]
                conn.execute("UPDATE pending_actions SET status = 'failed', failure = %s WHERE action_id = %s", ("; ".join(why)[:500], action_id))
                audit.record_in(conn, "action_failed", request_id=request_id, principal=principal, ref=action_id, status=a["kind"])
                later = Invalid("it can no longer be created", why)
            else:
                if a["kind"] == "leave_request":
                    ref = domain.create_leave_request(principal, args, "pending_manager", action_id=action_id, conn=conn)
                else:
                    ref = domain.create_ticket(principal, args, action_id=action_id, conn=conn)
                conn.execute("UPDATE pending_actions SET status = 'executed', confirmed_at = now(), result_ref = %s WHERE action_id = %s", (ref, action_id))
                audit.record_in(conn, "action_executed", request_id=request_id, principal=principal, ref=action_id, result_ref=ref, status=a["kind"])
                a = _action(conn.execute(f"SELECT {ACTION_COLUMNS} FROM pending_actions WHERE action_id = %s", (action_id,)).fetchone())
                return {"action": a, "result": _result(conn, a), "already_executed": False}
    raise later


def cancel_action(principal: Principal, action_id: str, request_id: str = "-") -> dict:
    with connect() as conn:
        _lock_person(conn, principal.user_id)
        row = conn.execute(f"SELECT {ACTION_COLUMNS} FROM pending_actions WHERE action_id = %s AND user_id = %s AND tenant_id = %s FOR UPDATE",
                           (str(action_id)[:40], principal.user_id, principal.tenant)).fetchone()
        if row is None:
            raise NotFound("no such action")
        a = _action(row)
        if a["status"] == "cancelled":
            return a
        if a["status"] != "awaiting_requester":
            raise Conflict(f"this action is {a['status']}: it can no longer be cancelled here")
        conn.execute("UPDATE pending_actions SET status = 'cancelled' WHERE action_id = %s", (action_id,))
        audit.record_in(conn, "action_cancelled", request_id=request_id, principal=principal, ref=action_id, status=a["kind"])
        a["status"] = "cancelled"
    return a


# ---------------- 3. a manager decides ----------------
LEAVE_COLUMNS = "request_id, user_id, tenant_id, leave_type, start_date, end_date, days, reason, status"


def leave_fingerprint(row) -> str:
    """What a manager is shown, fixed: if any of it changes between 'seen' and 'approved', the approval is refused."""
    body = json.dumps([str(x) for x in row[:9]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _require_manager(principal: Principal) -> None:
    if "manager" not in principal.roles:
        raise NotAllowed("only a manager can decide leave requests")


def list_approvals(principal: Principal, limit: int = 50) -> list[dict]:
    """The manager's inbox: leave requests of this company waiting for a decision, never the manager's own."""
    _require_manager(principal)
    with connect() as conn:
        rows = conn.execute(f"SELECT r.{LEAVE_COLUMNS.replace(', ', ', r.')}, u.username FROM leave_requests r JOIN users u ON u.user_id = r.user_id "
                            "WHERE r.tenant_id = %s AND r.status = 'pending_manager' AND r.user_id <> %s ORDER BY r.created_at, r.request_id LIMIT %s",
                            (principal.tenant, principal.user_id, min(int(limit), 50))).fetchall()
    return [{"request_id": r[0], "requester": r[9], "leave_type": r[3], "start_date": r[4].isoformat(), "end_date": r[5].isoformat(), "days": float(r[6]),
             "reason": r[7], "status": r[8], "fingerprint": leave_fingerprint(r)} for r in rows]


def decide_leave(principal: Principal, leave_id: str, decision: str, note: str, fingerprint: str, request_id: str = "-") -> dict:
    _require_manager(principal)
    if decision not in ("approve", "reject"):
        raise Invalid("the decision must be approve or reject")
    problems, warnings = [], []
    note = domain.vet_text("note", note or "", 0, 300, problems, warnings)
    if problems:
        raise Invalid("the note was not accepted", problems)
    with connect() as conn:
        row = conn.execute(f"SELECT {LEAVE_COLUMNS} FROM leave_requests WHERE request_id = %s AND tenant_id = %s FOR UPDATE", (str(leave_id)[:40], principal.tenant)).fetchone()
        if row is None:
            raise NotFound("no such leave request")
        if row[1] == principal.user_id:
            raise NotAllowed("you cannot decide your own request")
        if row[8] != "pending_manager":
            raise Conflict(f"this request is already {row[8]}")
        if fingerprint != leave_fingerprint(row):
            raise FingerprintMismatch("the request you saw is not the one that is stored")
        new_status = "approved" if decision == "approve" else "rejected"
        conn.execute("UPDATE leave_requests SET status = %s, decided_by = %s, decided_at = now(), decision_note = %s WHERE request_id = %s",
                     (new_status, principal.user_id, note or None, leave_id))
        audit.record_in(conn, "leave_decided", request_id=request_id, principal=principal, ref=leave_id, status=new_status)
    return {"request_id": leave_id, "status": new_status}


def cancel_leave(principal: Principal, leave_id: str, request_id: str = "-") -> dict:
    """The requester withdraws a leave request that is still waiting for a manager."""
    with connect() as conn:
        _lock_person(conn, principal.user_id)
        row = conn.execute(f"SELECT {LEAVE_COLUMNS} FROM leave_requests WHERE request_id = %s AND user_id = %s AND tenant_id = %s FOR UPDATE",
                           (str(leave_id)[:40], principal.user_id, principal.tenant)).fetchone()
        if row is None:
            raise NotFound("no such leave request")
        if row[8] == "cancelled":
            return {"request_id": leave_id, "status": "cancelled"}
        if row[8] != "pending_manager":
            raise Conflict(f"this request is already {row[8]} and cannot be cancelled here")
        conn.execute("UPDATE leave_requests SET status = 'cancelled' WHERE request_id = %s", (leave_id,))
        audit.record_in(conn, "leave_cancelled", request_id=request_id, principal=principal, ref=leave_id, status="cancelled")
    return {"request_id": leave_id, "status": "cancelled"}
