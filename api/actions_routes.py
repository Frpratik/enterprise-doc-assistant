"""
HTTP endpoints for actions and approvals (Phase 7, Stage 2). Everything needs a signed-in person (the same login token as /ask), and the person is
the ONLY thing that can confirm: there is no tool the model can call that reaches these endpoints.

  POST /actions/leave                  propose a leave request       (a valid draft becomes a PENDING action; nothing is created yet)
  POST /actions/ticket                 propose an IT ticket
  GET  /actions[?status=]              my pending and finished actions
  GET  /actions/{id}                   one of mine (someone else's looks like one that does not exist)
  POST /actions/{id}/confirm           the requester confirms, sending the fingerprint of the draft they were shown
  POST /actions/{id}/cancel            the requester withdraws it
  GET  /approvals                      a MANAGER's inbox: leave requests of the company waiting for a decision (never their own)
  POST /approvals/{leave_id}/decide    a MANAGER approves or rejects, sending the fingerprint of what they were shown
  POST /leave/{leave_id}/cancel        the requester withdraws a leave request that still waits for a manager
  POST /agent                          talk to the assistant that can look things up and PROPOSE (it never submits): the reply comes with the
                                       pending actions it prepared, each of which the person must confirm themselves
"""
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import time

from actions import agent as agent_mod, domain, workflow
from actions.workflow import ActionError
from api import main
from indexing import ratelimit
from indexing.access import Principal

router = APIRouter()
Fingerprint = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposeLeave(Strict):
    leave_type: Literal["sick", "casual", "earned"]
    start_date: str = Field(..., min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(..., min_length=10, max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = Field(..., min_length=3, max_length=500)


class ProposeTicket(Strict):
    category: Literal["hardware", "software", "access", "network", "other"]
    title: str = Field(..., min_length=3, max_length=120)
    description: str = Field(..., min_length=3, max_length=2000)
    priority: Literal["low", "normal", "high"] = "normal"


class Confirm(Strict):
    fingerprint: str = Fingerprint


class Decide(Strict):
    decision: Literal["approve", "reject"]
    note: str = Field("", max_length=300)
    fingerprint: str = Fingerprint


class AgentRequest(Strict):
    message: str = Field(..., min_length=1, max_length=1000)


def agent_gate(principal: Principal = Depends(main.current_principal)) -> Principal:
    """Every agent run costs several LLM calls: its own, smaller budget (5 a minute and 40 a day by default)."""
    ratelimit.get_limiter().agent_person(principal.user_id)
    return principal


def actions_gate(principal: Principal = Depends(main.current_principal)) -> Principal:
    """Every action request counts against the person's budget (30 a minute by default)."""
    ratelimit.get_limiter().action_person(principal.user_id)
    return principal


STATUS = {"not_found": 404, "conflict": 409, "expired": 410, "fingerprint_mismatch": 409, "not_allowed": 403, "invalid": 422}


def handle(request: Request, e: ActionError) -> JSONResponse:
    main.log.info("action_refused", request_id=main._rid(request), code=e.code)
    return JSONResponse(status_code=STATUS.get(e.code, 400), content={"detail": str(e), "code": e.code, "problems": e.problems, "request_id": main._rid(request)})


def _propose(request: Request, principal: Principal, draft) -> JSONResponse | dict:
    try:
        action = workflow.submit(principal, draft, main._rid(request))
    except ActionError as e:
        return handle(request, e)
    return {"action": action, "warnings": draft.warnings, "request_id": main._rid(request)}


@router.post("/actions/leave", status_code=201)
def propose_leave(body: ProposeLeave, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    return _propose(request, principal, domain.propose_leave(principal, body.leave_type, body.start_date, body.end_date, body.reason))


@router.post("/actions/ticket", status_code=201)
def propose_ticket(body: ProposeTicket, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    return _propose(request, principal, domain.propose_ticket(principal, body.category, body.title, body.description, body.priority))


@router.get("/actions")
def list_actions(request: Request, status: Literal["awaiting_requester", "executed", "cancelled", "expired", "failed"] | None = None,
                 _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    return {"actions": workflow.list_actions(principal, status), "request_id": main._rid(request)}


@router.get("/actions/{action_id}")
def get_action(action_id: str, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    try:
        return {"action": workflow.get_action(principal, action_id), "request_id": main._rid(request)}
    except ActionError as e:
        return handle(request, e)


@router.post("/actions/{action_id}/confirm")
def confirm_action(action_id: str, body: Confirm, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    try:
        return {**workflow.confirm(principal, action_id, body.fingerprint, main._rid(request)), "request_id": main._rid(request)}
    except ActionError as e:
        return handle(request, e)


@router.post("/actions/{action_id}/cancel")
def cancel_action(action_id: str, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    try:
        return {"action": workflow.cancel_action(principal, action_id, main._rid(request)), "request_id": main._rid(request)}
    except ActionError as e:
        return handle(request, e)


@router.get("/approvals")
def approvals(request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    try:
        return {"approvals": workflow.list_approvals(principal), "request_id": main._rid(request)}
    except ActionError as e:
        return handle(request, e)


@router.post("/approvals/{leave_id}/decide")
def decide(leave_id: str, body: Decide, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    try:
        return {**workflow.decide_leave(principal, leave_id, body.decision, body.note, body.fingerprint, main._rid(request)), "request_id": main._rid(request)}
    except ActionError as e:
        return handle(request, e)


@router.post("/leave/{leave_id}/cancel")
def cancel_leave(leave_id: str, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(actions_gate)):
    try:
        return {**workflow.cancel_leave(principal, leave_id, main._rid(request)), "request_id": main._rid(request)}
    except ActionError as e:
        return handle(request, e)


@router.post("/agent")
def agent(body: AgentRequest, request: Request, _ip: None = Depends(main.ip_gate), principal: Principal = Depends(agent_gate)):
    rid, start = main._rid(request), time.perf_counter()
    question = main._mask_question(body.message, rid)                    # personal data is replaced HERE, as for /ask: the model, the logs and the audit never see it
    agent_mod.audit_started(principal, rid, len(body.message), question.kinds)       # no record = no run
    with ratelimit.get_limiter().slot(principal.user_id):
        result = agent_mod.run_agent(principal, question.text, request_id=rid)
    ms = int((time.perf_counter() - start) * 1000)
    try:
        agent_mod.audit_finished(principal, rid, result, ms)
    except Exception as e:                                               # the run already happened and its proposals are audited one by one
        main.log.error("audit_write_failed", audit_event="agent_finished", error=f"{type(e).__name__}: {str(e)[:120]}")
    main.log.info("agent_run", request_id=rid, user_id=principal.user_id, status=result.status, steps=result.steps, tools=[t["name"] for t in result.tool_calls],
                  proposals=len(result.proposals), ms=ms)
    return {"reply": result.reply, "status": result.status, "proposals": result.proposals, "notes": result.notes, "tools": [t["name"] for t in result.tool_calls],
            "masked": question.kinds, "guard": result.guard, "request_id": rid}
