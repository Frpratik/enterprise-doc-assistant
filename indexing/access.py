"""
Access labels: who may see which document (Phase 5, Stage 1).

  tenant     which company the document belongs to (a company never sees another company's documents)
  roles      which roles inside that company may see it:  hr, finance, manager, ...   or the word  all  = every signed-in person

Where a label comes from, strongest first:
  1. MANUAL    a person ran  python -m indexing.manage set-access DOC_ID --roles hr,finance   (survives new versions of the file)
  2. RULE      the first matching pattern in access_rules.json (matched on the file name)
  3. nothing   the document is UNLABELLED: invisible to everyone until someone labels it       (FAIL CLOSED)

Design rules
  * Fail closed. A forgotten label hides a document; it never publishes one. The database itself refuses a half-labelled document (migration 004).
  * A typo is an ERROR, not a silent mistake: an unknown role in a rule stops loading the rules ('hr_' would otherwise hide the document from everyone).
  * One source of truth: the label lives on the documents table; every chunk inherits it through the join every search makes.
  * Labels are metadata only: changing one never re-reads or re-embeds a document.
  * No magic super-role: 'admin' is an ordinary role. An IT admin sees HR compensation only if the document says so (least privilege).

access_rules.json
  {"version": 1, "rules": [ {"match": "salary_*.xlsx", "tenant": "acme", "roles": ["hr", "finance"], "why": "compensation"}, ... ]}
  The FIRST rule that matches the file name (case-insensitive, * and ? work) wins. There is deliberately no catch-all in the sample file.
"""
import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from indexing.db import connect
from ingestion.config import settings

ROLES = ("admin", "finance", "hr", "it", "manager", "procurement")
ALL = "all"                      # every signed-in person of the tenant
UNASSIGNED = "unassigned"        # the tenant of a document nobody has labelled
_TENANT = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class AccessError(ValueError):
    """A rule or a label is invalid. Always loud: a silent mistake here would hide or publish documents."""


# ---------------- validation ----------------
def parse_roles(value) -> tuple[str, ...]:
    """'hr, Finance' or ['hr', 'finance']  ->  ('finance', 'hr'). Unknown roles, empty lists and 'all' mixed with others are errors."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise AccessError("roles must be a list or a comma-separated string")
    roles = sorted({str(r).strip().lower() for r in items if str(r).strip()})
    if not roles:
        raise AccessError("a document needs at least one role (or 'all')")
    unknown = [r for r in roles if r != ALL and r not in ROLES]
    if unknown:
        raise AccessError(f"unknown role(s) {unknown}; known roles: {', '.join(ROLES)} (or 'all')")
    if ALL in roles and len(roles) > 1:
        raise AccessError("'all' means everyone: it cannot be combined with other roles")
    return tuple(roles)


def validate_tenant(tenant) -> str:
    if not isinstance(tenant, str) or not _TENANT.match(tenant) or tenant == UNASSIGNED:
        raise AccessError(f"invalid tenant {tenant!r}: use 2-32 lowercase letters, digits, '-' or '_', starting with a letter (and not '{UNASSIGNED}')")
    return tenant


@dataclass(frozen=True)
class Label:
    tenant: str
    roles: tuple[str, ...]
    source: str                              # 'rule' or 'manual'

    def __post_init__(self):
        validate_tenant(self.tenant)
        object.__setattr__(self, "roles", parse_roles(list(self.roles)))
        if self.source not in ("rule", "manual"):
            raise AccessError(f"a label comes from a 'rule' or from a 'manual' action, not {self.source!r}")

    def describe(self) -> str:
        return f"{self.tenant}: {'everyone' if self.roles == (ALL,) else ', '.join(self.roles)}"


@dataclass(frozen=True)
class Rule:
    pattern: str
    tenant: str
    roles: tuple[str, ...]
    why: str = ""


# ---------------- the rules file ----------------
def load_rules(path: Path | None = None) -> list[Rule]:
    """Read and VALIDATE access_rules.json. A missing file means no rules: every document stays unlabelled (fail closed)."""
    path = Path(path or settings.access_rules_path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise AccessError(f"{path.name} is not valid JSON: {e}") from e
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("rules"), list):
        raise AccessError(f'{path.name} must look like {{"version": 1, "rules": [...]}}')
    rules, seen = [], set()
    for i, item in enumerate(raw["rules"], 1):
        where = f"rule {i} ({item.get('match') if isinstance(item, dict) else item!r})"
        if not isinstance(item, dict) or not isinstance(item.get("match"), str) or not item["match"].strip():
            raise AccessError(f"{where}: needs a 'match' pattern")
        pattern = item["match"].strip().lower()
        try:
            rule = Rule(pattern, validate_tenant(item.get("tenant")), parse_roles(item.get("roles", [])), str(item.get("why", "")))
        except AccessError as e:
            raise AccessError(f"{where}: {e}") from e
        if pattern in seen:
            raise AccessError(f"{where}: the pattern appears twice (the second rule could never apply)")
        seen.add(pattern)
        rules.append(rule)
    return rules


def is_catch_all(pattern: str) -> bool:
    return pattern.strip("*?") == ""


def match_rule(file_name: str, rules: list[Rule]) -> Rule | None:
    name = file_name.lower()
    return next((r for r in rules if fnmatch.fnmatchcase(name, r.pattern)), None)


@dataclass
class RulesFile:
    """The rules, reloaded whenever the file changes (so an edit takes effect without restarting a worker)."""
    _stamp: tuple = field(default=None, repr=False)
    _rules: list = field(default_factory=list, repr=False)

    def get(self) -> list[Rule]:
        path = Path(settings.access_rules_path)
        stamp = (str(path), path.stat().st_mtime_ns if path.exists() else None)
        if stamp != self._stamp:
            self._rules, self._stamp = load_rules(path), stamp
        return self._rules


# ---------------- labels in the database (all take an open connection: the caller decides the transaction) ----------------
def resolve_label(conn, doc_id: str, file_name: str, rules: list[Rule]) -> Label | None:
    """The label a NEW version of this document gets: a person's earlier decision, else the rules, else None (unlabelled)."""
    prev = conn.execute("SELECT tenant_id, allowed_roles FROM documents WHERE doc_id = %s AND access_source = 'manual' "
                        "AND access_status = 'labelled' ORDER BY version DESC LIMIT 1", (doc_id,)).fetchone()
    if prev:
        return Label(prev[0], tuple(prev[1]), "manual")
    rule = match_rule(file_name, rules)
    return Label(rule.tenant, rule.roles, "rule") if rule else None


def label_columns(label: Label | None) -> tuple:
    """(tenant_id, allowed_roles, access_status, access_source) for an INSERT or UPDATE."""
    if label is None:
        return (UNASSIGNED, [], "unlabelled", "none")
    return (label.tenant, list(label.roles), "labelled", label.source)


def set_label(conn, doc_id: str, label: Label | None) -> int:
    """Label (or, with None, un-label = hide) EVERY version of a document, in the caller's transaction. Returns the number of rows."""
    t, roles, status, source = label_columns(label)
    return conn.execute("UPDATE documents SET tenant_id = %s, allowed_roles = %s, access_status = %s, access_source = %s, "
                        "access_updated_at = now() WHERE doc_id = %s", (t, roles, status, source, doc_id)).rowcount


def set_access(doc_id: str, tenant: str, roles, unlabel: bool = False) -> dict:
    """A person labels (or hides) one document. Survives new versions of the file."""
    label = None if unlabel else Label(tenant, parse_roles(roles), "manual")
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM documents WHERE doc_id = %s AND deleted_at IS NULL", (doc_id,)).fetchone():
            raise AccessError(f"no document {doc_id}")
        rows = set_label(conn, doc_id, label)
    return {"doc_id": doc_id, "versions": rows, "label": label.describe() if label else "UNLABELLED (hidden from everyone)"}


def apply_rules(rules: list[Rule] | None = None) -> dict:
    """
    Re-apply the rules to every live document that was NOT labelled by a person. Rules are authoritative for those:
    a document whose rule was removed becomes unlabelled (hidden). Everything happens in ONE transaction.
    """
    rules = load_rules() if rules is None else rules
    counts = {"labelled": 0, "relabelled": 0, "hidden": 0, "unchanged": 0, "kept_manual": 0}
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT ON (doc_id) doc_id, file_name, tenant_id, allowed_roles, access_status, access_source "
                            "FROM documents WHERE deleted_at IS NULL ORDER BY doc_id, version DESC").fetchall()
        for doc_id, name, tenant, roles, status, source in rows:
            if source == "manual":
                counts["kept_manual"] += 1
                continue
            rule = match_rule(name, rules)
            new = Label(rule.tenant, rule.roles, "rule") if rule else None
            current = (tenant, tuple(roles), status)
            wanted = (new.tenant, new.roles, "labelled") if new else (UNASSIGNED, (), "unlabelled")
            if current == wanted:
                counts["unchanged"] += 1
                continue
            set_label(conn, doc_id, new)
            counts["hidden" if new is None else ("labelled" if status == "unlabelled" else "relabelled")] += 1
    return counts


def unlabelled_documents() -> list[tuple[str, str]]:
    """(doc_id, file name) of every live document nobody can see because it has no label."""
    with connect() as conn:
        return conn.execute("SELECT DISTINCT ON (doc_id) doc_id, file_name FROM documents WHERE deleted_at IS NULL AND status <> 'failed' "
                            "AND access_status = 'unlabelled' ORDER BY doc_id, version DESC").fetchall()


def access_report() -> list[tuple]:
    """(file name, doc_id, tenant, roles, status, source) of every live document, latest version."""
    with connect() as conn:
        return conn.execute("SELECT DISTINCT ON (doc_id) file_name, doc_id, tenant_id, allowed_roles, access_status, access_source "
                            "FROM documents WHERE deleted_at IS NULL ORDER BY doc_id, version DESC").fetchall()
