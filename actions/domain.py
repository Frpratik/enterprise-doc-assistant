"""
The leave and ticket rules of the (simulated) company, in one place.

  READS     what a person may look at: their own balances, requests and tickets (never another person's, never another company's)
  DRAFTS    propose_leave() / propose_ticket(): check everything a real system would check and return what WOULD be created, plus the reasons
            it cannot be. A draft changes NOTHING in the database. Its `fingerprint` ties it to the exact arguments, the person and the
            company: Stage 2 stores it and a person approves THAT fingerprint, so what runs is exactly what was approved.
  WRITES    create_leave_request() / create_ticket(): used only AFTER a person approved a draft (Stage 2) and by tests. They are not
            reachable from any tool the model can call.

Text that goes into a system (a ticket title, a leave reason) is cleaned (control and hidden characters removed, spaces collapsed), personal data
in it is masked (the ticket system does not need an Aadhaar number), and text that reads like instructions to a machine is refused.
"""
import calendar
import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from indexing import injection, pii
from indexing.access import Principal
from indexing.db import connect
from ingestion.config import settings

LEAVE_TYPES = ("sick", "casual", "earned")
ANNUAL_DAYS = {"sick": 12.0, "casual": 8.0, "earned": 18.0}                 # as in the leave policy
MAX_DAYS_PER_REQUEST = {"sick": 10, "casual": 3, "earned": 15}
NOTICE_DAYS = {"sick": 0, "casual": 1, "earned": 7}                          # a request must start at least this many days from today
SICK_BACKDATE_DAYS = 3                                                       # sick leave may be applied for up to 3 days after it began
CATEGORIES = ("hardware", "software", "access", "network", "other")
PRIORITIES = ("low", "normal", "high")
TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")
LEAVE_STATUSES = ("pending_manager", "approved", "rejected", "cancelled")
MAX_LIST = 20


@dataclass
class Draft:
    kind: str                                    # 'leave_request' | 'ticket'
    args: dict                                   # the normalised arguments: exactly what would be created
    valid: bool = True
    problems: list = field(default_factory=list)  # why it cannot be created (each a plain sentence)
    warnings: list = field(default_factory=list)  # things the person should know (masked personal data, a low balance)
    summary: str = ""
    fingerprint: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "args": self.args, "valid": self.valid, "problems": self.problems, "warnings": self.warnings,
                "summary": self.summary, "fingerprint": self.fingerprint}


def fingerprint(kind: str, args: dict, principal: Principal) -> str:
    """Ties a draft to its exact arguments AND to the person and the company it was made for: the same arguments for someone else differ."""
    body = json.dumps({"kind": kind, "args": args, "user": principal.user_id, "tenant": principal.tenant}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------- text that goes into a system ----------------
def clean_text(text: str) -> str:
    """Control and hidden characters removed, compatibility forms folded, spaces collapsed."""
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(c for c in text if c in "\n\t " or unicodedata.category(c)[0] != "C")
    return " ".join(text.split())


def vet_text(label: str, text: str, minimum: int, maximum: int, problems: list, warnings: list) -> str:
    """Clean, mask personal data, check the length, refuse instruction-like text. Returns the text that would be stored."""
    text = clean_text(text)
    masked = pii.mask(text)
    if masked.found:
        warnings.append(f"personal data in the {label} ({', '.join(masked.kinds)}) was replaced by placeholders: the system does not need it")
        text = masked.text
    if injection.scan(text).hostile:
        problems.append(f"the {label} reads like instructions to a machine and was not accepted")
    if len(text) < minimum:
        problems.append(f"the {label} is too short (at least {minimum} characters)")
    if len(text) > maximum:
        problems.append(f"the {label} is too long ({len(text)} characters, at most {maximum})")
    return text


# ---------------- working days ----------------
def holidays() -> set[date]:
    return {date.fromisoformat(d) for d in settings.company_holidays}


def working_days(start: date, end: date) -> float:
    """Monday to Friday, without the company holidays."""
    off, n, d = holidays(), 0, start
    while d <= end:
        if d.weekday() < 5 and d not in off:
            n += 1
        d += timedelta(days=1)
    return float(n)


def parse_date(value: str, label: str, problems: list) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        problems.append(f"the {label} must be a date like 2026-12-07")
        return None


# ---------------- balances and reads ----------------
def ensure_balances(user_id: str, year: int) -> None:
    with connect() as conn:
        for leave_type, days in ANNUAL_DAYS.items():
            conn.execute("INSERT INTO leave_balances (user_id, leave_type, year, annual_days) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (user_id, leave_type, year, days))


def balances(user_id: str, year: int | None = None) -> dict[str, dict]:
    """{leave_type: {annual, taken, pending, remaining}}. Days of a request waiting for the manager are RESERVED (they count against 'remaining')."""
    year = year or date.today().year
    ensure_balances(user_id, year)
    with connect() as conn:
        annual = dict(conn.execute("SELECT leave_type, annual_days FROM leave_balances WHERE user_id = %s AND year = %s", (user_id, year)).fetchall())
        used = conn.execute("SELECT leave_type, status, coalesce(sum(days), 0) FROM leave_requests WHERE user_id = %s AND status IN ('approved', 'pending_manager') "
                            "AND date_part('year', start_date) = %s GROUP BY leave_type, status", (user_id, year)).fetchall()
    out = {t: {"annual": float(a), "taken": 0.0, "pending": 0.0} for t, a in annual.items()}
    for leave_type, status, days in used:
        out[leave_type]["taken" if status == "approved" else "pending"] += float(days)
    for v in out.values():
        v["remaining"] = round(v["annual"] - v["taken"] - v["pending"], 1)
    return out


def _leave_row(r) -> dict:
    return {"request_id": r[0], "leave_type": r[1], "start_date": r[2].isoformat(), "end_date": r[3].isoformat(), "days": float(r[4]), "reason": r[5], "status": r[6]}


def my_leave_requests(user_id: str, status: str | None = None, limit: int = MAX_LIST) -> list[dict]:
    """The person's OWN requests, newest first (at most MAX_LIST)."""
    if status is not None and status not in LEAVE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(LEAVE_STATUSES)}")
    with connect() as conn:
        rows = conn.execute("SELECT request_id, leave_type, start_date, end_date, days, reason, status FROM leave_requests "
                            "WHERE user_id = %s AND (%s::text IS NULL OR status = %s) ORDER BY created_at DESC, request_id LIMIT %s",
                            (user_id, status, status, min(int(limit), MAX_LIST))).fetchall()
    return [_leave_row(r) for r in rows]


def _ticket_row(r) -> dict:
    return {"ticket_id": r[0], "category": r[1], "priority": r[2], "title": r[3], "description": r[4], "status": r[5], "created_at": r[6].isoformat(timespec="seconds")}


TICKET_COLUMNS = "ticket_id, category, priority, title, description, status, created_at"


def my_tickets(user_id: str, status: str | None = None, limit: int = MAX_LIST) -> list[dict]:
    if status is not None and status not in TICKET_STATUSES:
        raise ValueError(f"status must be one of {', '.join(TICKET_STATUSES)}")
    with connect() as conn:
        rows = conn.execute(f"SELECT {TICKET_COLUMNS} FROM tickets WHERE user_id = %s AND (%s::text IS NULL OR status = %s) ORDER BY created_at DESC, ticket_id LIMIT %s",
                            (user_id, status, status, min(int(limit), MAX_LIST))).fetchall()
    return [_ticket_row(r) for r in rows]


def get_my_ticket(user_id: str, ticket_id: str) -> dict | None:
    """One ticket, only if it is the caller's own. Someone else's ticket looks exactly like a ticket that does not exist."""
    with connect() as conn:
        row = conn.execute(f"SELECT {TICKET_COLUMNS} FROM tickets WHERE ticket_id = %s AND user_id = %s", (str(ticket_id)[:40], user_id)).fetchone()
    return _ticket_row(row) if row else None


# ---------------- drafts: check everything, change nothing ----------------
def propose_leave(principal: Principal, leave_type: str, start_date: str, end_date: str, reason: str, today: date | None = None) -> Draft:
    today = today or date.today()
    problems, warnings = [], []
    if leave_type not in LEAVE_TYPES:
        problems.append(f"the leave type must be one of {', '.join(LEAVE_TYPES)}")
    start, end = parse_date(start_date, "start date", problems), parse_date(end_date, "end date", problems)
    reason = vet_text("reason", reason, 3, 500, problems, warnings)
    days = 0.0
    if start and end and leave_type in LEAVE_TYPES:
        if end < start:
            problems.append("the end date is before the start date")
        elif start.year != end.year:
            problems.append("a request cannot span two calendar years: make one request for each year")
        else:
            days = working_days(start, end)
            if days == 0:
                problems.append("there are no working days in that period (weekends and company holidays do not count)")
            if days > MAX_DAYS_PER_REQUEST[leave_type]:
                problems.append(f"{leave_type} leave is limited to {MAX_DAYS_PER_REQUEST[leave_type]} working days per request ({days:g} asked)")
            earliest = today - timedelta(days=SICK_BACKDATE_DAYS) if leave_type == "sick" else today + timedelta(days=NOTICE_DAYS[leave_type])
            if start < earliest:
                problems.append("sick leave can be applied for at most 3 days after it began" if leave_type == "sick"
                                else f"{leave_type} leave must start at least {NOTICE_DAYS[leave_type]} day(s) from today")
            if start > today + timedelta(days=365):
                problems.append("a request cannot start more than a year from today")
            if days > 0 and not problems:
                bal = balances(principal.user_id, start.year)[leave_type]
                if days > bal["remaining"]:
                    problems.append(f"not enough {leave_type} leave: {bal['remaining']:g} day(s) left, {days:g} asked")
                elif bal["remaining"] - days <= 1:
                    warnings.append(f"only {bal['remaining'] - days:g} day(s) of {leave_type} leave would be left")
                with connect() as conn:
                    clash = conn.execute("SELECT request_id FROM leave_requests WHERE user_id = %s AND status IN ('approved', 'pending_manager') "
                                         "AND start_date <= %s AND end_date >= %s LIMIT 1", (principal.user_id, end, start)).fetchone()
                if clash:
                    problems.append(f"it overlaps your request {clash[0]}")
    args = {"leave_type": leave_type, "start_date": start.isoformat() if start else str(start_date)[:10], "end_date": end.isoformat() if end else str(end_date)[:10],
            "reason": reason, "days": days}
    summary = ""
    if not problems:
        summary = f"{leave_type.capitalize()} leave from {calendar.day_abbr[start.weekday()]} {start} to {calendar.day_abbr[end.weekday()]} {end} ({days:g} working day(s)). It then waits for your manager."
    return Draft("leave_request", args, valid=not problems, problems=problems, warnings=warnings, summary=summary, fingerprint=fingerprint("leave_request", args, principal))


def propose_ticket(principal: Principal, category: str, title: str, description: str, priority: str = "normal") -> Draft:
    problems, warnings = [], []
    if category not in CATEGORIES:
        problems.append(f"the category must be one of {', '.join(CATEGORIES)}")
    if priority not in PRIORITIES:
        problems.append(f"the priority must be one of {', '.join(PRIORITIES)}")
    title = vet_text("title", title, 3, 120, problems, warnings)
    description = vet_text("description", description, 3, 2000, problems, warnings)
    args = {"category": category, "priority": priority, "title": title, "description": description}
    summary = "" if problems else f"IT ticket ({category}, {priority} priority): {title}"
    return Draft("ticket", args, valid=not problems, problems=problems, warnings=warnings, summary=summary, fingerprint=fingerprint("ticket", args, principal))


# ---------------- writes: only after a person approved a draft (Stage 2), and in tests ----------------
def create_leave_request(principal: Principal, args: dict, status: str = "pending_manager", decided_by: str | None = None) -> str:
    request_id = "LV-" + uuid.uuid4().hex[:8].upper()
    with connect() as conn:
        conn.execute("INSERT INTO leave_requests (request_id, user_id, tenant_id, leave_type, start_date, end_date, days, reason, status, decided_by, decided_at) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s::text IS NULL THEN NULL ELSE now() END)",
                     (request_id, principal.user_id, principal.tenant, args["leave_type"], args["start_date"], args["end_date"], args["days"], args["reason"], status,
                      decided_by, decided_by))
    return request_id


def create_ticket(principal: Principal, args: dict) -> str:
    with connect() as conn:
        return conn.execute("INSERT INTO tickets (user_id, tenant_id, category, priority, title, description) VALUES (%s,%s,%s,%s,%s,%s) RETURNING ticket_id",
                            (principal.user_id, principal.tenant, args["category"], args["priority"], args["title"], args["description"])).fetchone()[0]
