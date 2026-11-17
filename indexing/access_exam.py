"""
The access exam (Phase 5, Stage 5): does anyone ever get to see something they may not?

  python -m indexing.access_exam            exit 0 = no leak and nothing over-blocked, 1 = a problem

It asks the REAL pipeline (real search, real reranker, real score gate, real prompt, real answer checks) the questions that touch restricted
documents, as people with different roles in two companies, against a throwaway database indexed with the real `access_rules.json`.

For every (person, question) it collects everything that could leak:
  prompt      every message the LLM was given            answer      the text the person is shown (stream + final)
  citations   the sources shown with the answer           events      every stream event, serialised
and looks in them for FORBIDDEN material = things that exist only in documents this person may not read:
  file names and titles of those documents, their CANARY codes, and the gold evidence phrases of the question that live only there.
(Anything that also appears in a document the person CAN read is not counted: a visible document may legitimately mention a name.)

Two kinds of failure, both zero tolerance:
  LEAK         forbidden material reached a surface. (This is the 'forbidden evidence' rule of the regression gate.)
  OVER-BLOCK   a person who may read the evidence did not get it, although an all-roles auditor did. Without this check, a system that
               hides EVERYTHING would pass the leak check. The report also counts the real denials and the real servings, so a
               vacuous exam (nothing denied, nothing served) is itself a failure.

The LLM is a spy that records its prompt and quotes the first excerpt: the exam needs no API key and no quota, and what it quotes is exactly
what a model could repeat. The exam proves that nothing forbidden is put within the model's reach, which is the only thing we control.
"""
import json
import re
import runpy
import sys
from dataclasses import dataclass, field

from indexing import db, search_eval
from indexing.access import ALL, ROLES, Principal
from indexing.answer import answer_stream
from indexing.answer_golden import SAMPLES, contains, load_golden
from indexing.llm import LLMResult
from ingestion.config import PROJECT_ROOT

ACCESS_DIR = SAMPLES.parent / "access"
CANARY = re.compile(r"CANARY-[A-Z0-9-]+")


# ---------------- who and what ----------------
@dataclass(frozen=True)
class Person:
    name: str
    principal: Principal


PEOPLE = [Person("acme employee", Principal("acme", (), "u-emp"))] + \
         [Person(f"acme {r}", Principal("acme", (r,), f"u-{r}")) for r in ROLES] + \
         [Person("acme hr+finance", Principal("acme", ("finance", "hr"), "u-hrfin")),
          Person("globex employee", Principal("globex", (), "g-emp")), Person("globex hr", Principal("globex", ("hr",), "g-hr"))]


@dataclass
class ExamQuestion:
    id: str
    q: str
    tenant: str
    groups: list = field(default_factory=list)          # [[(file, phrase), ...alternatives], ...]: ALL groups must be found


EXTRA = [
    ExamQuestion("X001", "What acquisition price did the board approve?", "acme", [[("acme_board_minutes.md", "Rs 480 crore")]]),
    ExamQuestion("X002", "What is the CEO's annual package?", "acme", [[("acme_executive_compensation.md", "Rs 4.2 crore")]]),
    ExamQuestion("X003", "How many sick leaves do Globex employees get per year?", "globex", [[("globex_leave_policy.md", "20 sick leaves")]]),
    ExamQuestion("X004", "What does a grade G2 employee earn at Globex?", "globex", [[("globex_salary_bands.md", "Rs 10 to 16 LPA")]]),
    ExamQuestion("X005", "How many sick leaves do I get per year?", "acme", [[("leave_policy.pdf", "12 sick leaves")]]),   # same topic as Globex: tenant isolation
    ExamQuestion("X006", "Which positions will be cut in the Sales team?", "acme", [[("acme_board_minutes.md", "40 positions")]]),
]


def build_questions(golden=None) -> list[ExamQuestion]:
    """The 14 answer-exam questions whose evidence lives in a restricted document, 4 public ones as controls, and the extra canary questions."""
    from indexing.access import ALL, load_rules, match_rule
    golden = golden or load_golden()
    rules = load_rules(PROJECT_ROOT / "access_rules.json")
    public = {d for d in golden.documents if (match_rule(d, rules) and match_rule(d, rules).roles == (ALL,))}
    out, controls = [], 0
    for q in golden.questions:
        if not q.answerable or not q.sources:
            continue
        files = {f for grp in q.sources for f, _ in grp}
        restricted = bool(files - public)
        if restricted or controls < 4:
            controls += 0 if restricted else 1
            out.append(ExamQuestion(q.id, q.q, "acme", [[tuple(alt) for alt in grp] for grp in q.sources]))
    return out + EXTRA


# ---------------- the spy ----------------
def excerpts_of(messages) -> list[tuple[str, str]]:
    """What the LLM was really given: [(file name, excerpt text)] in prompt order."""
    user = messages[1]["content"]
    if "<excerpts>\n" not in user:
        return []
    body = user.split("<excerpts>\n", 1)[1].split("\n</excerpts>", 1)[0]
    out = []
    for block in re.split(r"\n\n(?=\[\d+\] )", body):
        header, _, text = block.partition("\n")
        m = re.match(r"\[\d+\] ([^,|]+)", header)
        out.append((m.group(1).strip() if m else "", text))
    return out


class SpyLLM:
    """Records every prompt and 'answers' by quoting the first excerpt with a citation: whatever is in the excerpts can reach the person."""
    def __init__(self):
        self.calls = []

    def _text(self, messages):
        blocks = excerpts_of(messages)
        first = blocks[0][1].strip().splitlines()[-1][:120] if blocks and blocks[0][1].strip() else "no text"
        return f"{first} [1]"

    def chat(self, messages, tier="main", **kw):
        self.calls.append(messages)
        return LLMResult(self._text(messages), "spy", tier, [], {}, 0)

    def stream(self, messages, tier="main", **kw):
        self.calls.append(messages)
        text = self._text(messages)
        for word in text.split(" "):
            yield "token", word + " "
        yield "done", LLMResult(text, "spy", tier, [], {}, 0)


# ---------------- what a person may see ----------------
def may_read(p: Principal, tenant: str, roles, status: str) -> bool:
    """The rule, written a SECOND time on purpose, in Python and not in SQL: if the search's SQL condition were wrong, this would still be right
    and the exam would see the difference. (Same company, labelled, and 'all' or one shared role.)"""
    return status == "labelled" and tenant == p.tenant and (ALL in roles or bool(set(roles) & set(p.roles)))


def _documents(conn):
    """(file name, title, tenant, roles, status) of every live document, latest version."""
    return conn.execute("SELECT DISTINCT ON (d.doc_id) d.file_name, coalesce(d.title, ''), d.tenant_id, d.allowed_roles, d.access_status "
                        "FROM documents d WHERE d.deleted_at IS NULL ORDER BY d.doc_id, d.version DESC").fetchall()


class World:
    """Everything the exam needs to know about the indexed corpus, read once."""
    def __init__(self):
        with db.connect() as conn:
            self.all_docs = {r[0]: r for r in _documents(conn)}                     # file name -> (name, title, tenant, roles, status)
            chunks = conn.execute("SELECT d.file_name, c.content FROM chunks c JOIN documents d ON d.doc_id = c.doc_id AND d.version = c.version "
                                  "WHERE c.is_active AND d.deleted_at IS NULL").fetchall()
        self.text_by_file: dict[str, str] = {}
        for name, content in chunks:
            self.text_by_file[name] = self.text_by_file.get(name, "") + "\n" + content
        self.canaries = {name: set(CANARY.findall(self.text_by_file.get(name, ""))) for name in self.all_docs}
        self._visible: dict = {}
        self._text: dict = {}

    def visible_files(self, p: Principal) -> set[str]:
        if p not in self._visible:
            self._visible[p] = {n for n, (_, _, tenant, roles, status) in self.all_docs.items() if may_read(p, tenant, roles, status)}
        return self._visible[p]

    def visible_text(self, p: Principal) -> str:
        if p not in self._text:
            self._text[p] = "\n".join(self.text_by_file.get(n, "") for n in sorted(self.visible_files(p))).lower()
        return self._text[p]

    def forbidden(self, p: Principal, q: ExamQuestion) -> dict[str, list[str]]:
        """Material that exists only in documents p may not read."""
        seen, text = self.visible_files(p), self.visible_text(p)
        hidden = [n for n in self.all_docs if n not in seen]
        out = {"file name": [], "title": [], "canary": [], "evidence": []}
        for n in hidden:
            if n.lower() not in text:
                out["file name"].append(n)
            title = self.all_docs[n][1]
            if len(title) >= 8 and title.lower() not in text:
                out["title"].append(title)
            out["canary"] += [c for c in self.canaries[n] if c.lower() not in text]
        for grp in q.groups:
            for f, phrase in grp:
                if f in hidden and not contains(text, phrase):
                    out["evidence"].append(phrase)
        return out


def _leaks_in(surface: str, forbidden: dict[str, list[str]]) -> list[tuple[str, str]]:
    low, found = surface.lower(), []
    for kind, values in forbidden.items():
        for v in values:
            if (contains(surface, v) if kind == "evidence" else v.lower() in low):
                found.append((kind, v))
    return found


def _got_evidence(blocks, q: ExamQuestion) -> bool:
    return all(any(f == file and contains(text, phrase) for file, text in blocks for f, phrase in grp) for grp in q.groups)


# ---------------- running ----------------
@dataclass
class ExamResult:
    questions: int = 0
    people: int = 0
    checks: int = 0                  # (person, question) pairs asked
    served: int = 0                  # pairs where the person may read all the evidence and received it
    denied: int = 0                  # pairs where the evidence is in documents the person may not read and none of it reached them
    leaks: list = field(default_factory=list)
    over_blocks: list = field(default_factory=list)
    per_person: dict = field(default_factory=dict)

    @property
    def problems(self) -> list[dict]:
        out = [{"check": "forbidden_evidence", "id": l["question"], "detail": f"{l['person']}: {l['kind']} '{l['value']}' reached the {l['surface']}"} for l in self.leaks]
        out += [{"check": "access_over_block", "id": o["question"], "detail": f"{o['person']} may read the evidence and did not get it (an auditor did)"} for o in self.over_blocks]
        if self.checks and not (self.served and self.denied):
            out.append({"check": "access_exam_vacuous", "id": "-", "detail": f"the exam proved nothing: {self.served} served, {self.denied} denied"})
        if not self.checks:
            out.append({"check": "access_exam_vacuous", "id": "-", "detail": "no question was asked"})
        return out

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {"questions": self.questions, "people": self.people, "checks": self.checks, "served": self.served, "denied": self.denied,
                "leaks": self.leaks, "over_blocks": self.over_blocks, "per_person": self.per_person}


def run_exam(questions: list[ExamQuestion], people: list[Person] = None, embedder=None, progress=None) -> ExamResult:
    """Ask every question as every person, in the database that is currently configured (already indexed)."""
    people = people or PEOPLE
    world, res = World(), ExamResult(questions=len(questions), people=len(people))
    auditor_got: dict = {}
    for q in questions:                                           # the positive control: could an all-roles person of that company get the evidence at all?
        spy = SpyLLM()
        list(answer_stream(q.q, embedder=embedder, llm=spy, principal=Principal.auditor(q.tenant)))
        auditor_got[q.id] = bool(spy.calls) and _got_evidence(excerpts_of(spy.calls[0]), q)
    for person in people:
        stats = res.per_person.setdefault(person.name, {"asked": 0, "served": 0, "denied": 0, "leaks": 0})
        for q in questions:
            spy = SpyLLM()
            events = list(answer_stream(q.q, embedder=embedder, llm=spy, principal=person.principal))
            done = next(d for e, d in reversed(events) if e == "done")
            prompt = "\n".join(m["content"] for call in spy.calls for m in call)
            blocks = excerpts_of(spy.calls[0]) if spy.calls else []
            surfaces = {"prompt": prompt, "answer": done["text"] + " " + "".join(d.get("text", "") for e, d in events if e == "token"),
                        "citations": json.dumps(done["citations"], ensure_ascii=False), "events": json.dumps(events, ensure_ascii=False)}
            forbidden = world.forbidden(person.principal, q)
            res.checks += 1
            stats["asked"] += 1
            for surface, text in surfaces.items():
                for kind, value in _leaks_in(text, forbidden):
                    res.leaks.append({"person": person.name, "question": q.id, "surface": surface, "kind": kind, "value": value})
                    stats["leaks"] += 1
            visible = world.visible_files(person.principal)
            reachable = all(any(f in visible for f, _ in grp) for grp in q.groups)
            hidden_evidence = bool(forbidden["evidence"])
            got = _got_evidence(blocks, q)
            if person.principal.tenant == q.tenant and reachable:
                if got:
                    res.served += 1
                    stats["served"] += 1
                elif auditor_got[q.id]:
                    res.over_blocks.append({"person": person.name, "question": q.id})
            elif hidden_evidence and not got:
                res.denied += 1
                stats["denied"] += 1
        if progress:
            progress(f"  {person.name}: {stats}")
    return res


def format_result(res: ExamResult) -> str:
    L = [f"\nACCESS EXAM: {'PASS' if res.ok else 'FAIL'}",
         f"  {res.questions} questions x {res.people} people = {res.checks} checks (prompt, answer, citations and stream events of each one inspected)",
         f"  served correctly: {res.served}   withheld correctly: {res.denied}   leaks: {len(res.leaks)}   over-blocked: {len(res.over_blocks)}",
         f"\n  {'PERSON':18} {'ASKED':>5} {'SERVED':>6} {'DENIED':>6} {'LEAKS':>5}"]
    L += [f"  {n:18} {s['asked']:>5} {s['served']:>6} {s['denied']:>6} {s['leaks']:>5}" for n, s in res.per_person.items()]
    for p in res.problems:
        L.append(f"  [{p['check']}] {p['id']}  {p['detail']}")
    return "\n".join(L)


def ensure_access_documents() -> None:
    if not ACCESS_DIR.exists() or not list(ACCESS_DIR.glob("*.md")):
        runpy.run_path(str(PROJECT_ROOT / "scripts" / "make_access_corpus.py"), run_name="__main__")


def run_full(progress=None) -> ExamResult:
    """Index the exam corpus (the 22 documents of the answer exam + the 4 canary documents) in a throwaway database, then run the exam."""
    from indexing.answer_golden import corpus_paths
    from ingestion.config import settings
    ensure_access_documents()
    golden = load_golden()
    paths = corpus_paths(golden.documents)
    names = [*golden.documents, *sorted(p.name for p in ACCESS_DIR.glob("*.md"))]
    paths.update({p.name: p for p in ACCESS_DIR.glob("*.md")})
    saved_rules = settings.access_rules_path
    settings.access_rules_path = PROJECT_ROOT / "access_rules.json"                # ALWAYS the real rules, whatever the caller configured (a test may point at its own)
    try:
        with search_eval.temp_database():
            search_eval.index_golden_documents(names, resolve=lambda n: paths[n])
            return run_exam(build_questions(golden), PEOPLE, progress=progress)
    finally:
        settings.access_rules_path = saved_rules


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from indexing.db import can_connect
    if not can_connect():
        print("Database not reachable: start it with  docker compose up -d")
        return 2
    res = run_full(progress=print)
    print(format_result(res))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
