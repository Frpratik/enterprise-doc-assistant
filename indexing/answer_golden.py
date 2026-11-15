"""
The golden set for the WHOLE answer pipeline (Phase 4): questions, reference answers, required facts, gold sources.

  evals/answers_golden.json   144 questions over 22 documents (the 12 original samples + 10 harder ones in data/samples/eval2)

Every question has
  q, type, difficulty, answerable, origin ("v1" = a Phase 1-3 question, "v2" = new in Phase 4)
  answerable questions:    reference (a correct answer), facts (what a correct answer MUST contain), sources (gold evidence)
  unanswerable questions:  forbidden_terms (words that must not appear anywhere in the documents)

  facts     ["26 weeks", "12 sick leaves || 12 sick days"]     every entry must appear in the answer; ' || ' separates
                                                                alternative wordings
  sources   a list of GROUPS; for each group, ONE chunk matching any of its [file, phrase] alternatives must be found.
            One group = a single-evidence question; several groups = a multi-chunk question.

validate() is the safety net for the answer key itself: a typo in a phrase, a fact that is not in its source, or a
new document that accidentally answers an "unanswerable" question is caught by a test, not by a confusing score.

  python -m indexing.answer_golden                 summary + validation
  python -m indexing.answer_golden --retrieval     also: how hard is it? (retrieval + gate per question type; needs Docker + models)
"""
import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from indexing.retrieval_eval import SAMPLES, load_document, norm
from ingestion.config import PROJECT_ROOT

GOLDEN = PROJECT_ROOT / "evals" / "answers_golden.json"
CORPUS_DIRS = [SAMPLES, SAMPLES.parent / "eval2"]          # SAMPLES is data/samples/good; the harder documents are in data/samples/eval2
ANSWERABLE_TYPES = {"paraphrase", "single_fact", "table_lookup", "lookalike", "multi_chunk", "procedural", "yes_no", "computed"}
UNANSWERABLE_TYPES = {"near_topic", "off_topic"}
DIFFICULTIES = {"easy", "medium", "hard"}


@dataclass
class Question:
    id: str
    q: str
    type: str
    answerable: bool
    origin: str
    difficulty: str = ""
    reference: str = ""
    facts: list[str] = field(default_factory=list)
    sources: list[list[list[str]]] = field(default_factory=list)
    forbidden_terms: list[str] = field(default_factory=list)
    computed: bool = False


@dataclass
class Golden:
    version: int
    documents: list[str]
    questions: list[Question]

    @property
    def answerable(self):
        return [q for q in self.questions if q.answerable]

    @property
    def unanswerable(self):
        return [q for q in self.questions if not q.answerable]


def load_golden(path: Path = GOLDEN) -> Golden:
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = set(Question.__dataclass_fields__)
    return Golden(raw["version"], raw["documents"], [Question(**{k: v for k, v in q.items() if k in known}) for q in raw["questions"]])


# ---------------- the documents ----------------
def corpus_paths(names: list[str]) -> dict[str, Path]:
    found, missing = {}, []
    for name in names:
        hits = [d / name for d in CORPUS_DIRS if (d / name).exists()]
        if len(hits) > 1:
            raise ValueError(f"{name} exists in more than one corpus folder")
        (found.__setitem__(name, hits[0]) if hits else missing.append(name))
    if missing:
        raise FileNotFoundError(f"missing documents {missing}. Create them with:  python scripts/make_sample_data.py   and   python scripts/make_eval_corpus.py")
    return found


def corpus_texts(golden: Golden) -> dict[str, str]:
    return {name: load_document(path).full_text() for name, path in corpus_paths(golden.documents).items()}


# ---------------- facts ----------------
def fact_alternatives(fact: str) -> list[str]:
    return [alt.strip() for alt in fact.split("||") if alt.strip()]


def contains(text: str, phrase: str) -> bool:
    """
    True if the phrase appears in the text as WHOLE WORDS, ignoring case and punctuation.
    Plain substring matching would find '12' inside '120' and '8' inside '18', and inflate every score.
    """
    needle = norm(phrase)
    return bool(needle) and f" {needle} " in f" {norm(text)} "


def fact_present(fact: str, text: str) -> bool:
    """True if any alternative wording of the fact appears in text (whole words, ignoring case and punctuation)."""
    return any(contains(text, alt) for alt in fact_alternatives(fact))


def fact_recall(facts: list[str], answer_text: str) -> float:
    """Share of the required facts that the answer contains (1.0 = all of them)."""
    return sum(fact_present(f, answer_text) for f in facts) / len(facts) if facts else 1.0


def term_present(term: str, text: str) -> bool:
    """Whole-word match: 'holi' must not match 'holiday'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text.lower()) is not None


# ---------------- the safety net for the answer key ----------------
def validate(golden: Golden, texts: dict[str, str]) -> list[str]:
    problems: list[str] = []
    ids = [q.id for q in golden.questions]
    if len(ids) != len(set(ids)):
        problems.append("duplicate question ids")
    lowered = [q.q.strip().lower() for q in golden.questions]
    problems += [f"duplicate question: {q}" for q, n in Counter(lowered).items() if n > 1]
    all_text = "\n".join(texts.values())

    for q in golden.questions:
        tag = f"{q.id} ({q.q[:50]})"
        if q.answerable:
            if q.type not in ANSWERABLE_TYPES:
                problems.append(f"{tag}: unknown type '{q.type}'")
            if q.difficulty not in DIFFICULTIES:
                problems.append(f"{tag}: difficulty must be easy / medium / hard")
            if not q.reference.strip() or not q.facts or not q.sources:
                problems.append(f"{tag}: needs reference, facts and sources")
                continue
            source_files = set()
            for group in q.sources:
                if not group:
                    problems.append(f"{tag}: empty source group")
                for file_name, phrase in group:
                    source_files.add(file_name)
                    if file_name not in texts:
                        problems.append(f"{tag}: source file {file_name} is not in the document list")
                    elif not contains(texts[file_name], phrase):
                        problems.append(f"{tag}: phrase '{phrase}' is not in {file_name}")
            if q.type == "multi_chunk" and len(q.sources) < 2:
                problems.append(f"{tag}: a multi_chunk question needs at least 2 source groups")
            source_text = "\n".join(texts.get(f, "") for f in source_files)
            for fact in q.facts:
                if not fact_present(fact, q.reference):
                    problems.append(f"{tag}: fact '{fact}' is not in the reference answer")
                if not q.computed and not fact_present(fact, source_text):
                    problems.append(f"{tag}: fact '{fact}' is not in its source documents (mark the question computed if it is calculated)")
        else:
            if q.type not in UNANSWERABLE_TYPES:
                problems.append(f"{tag}: unknown type '{q.type}'")
            if not q.forbidden_terms:
                problems.append(f"{tag}: needs forbidden_terms")
            for term in q.forbidden_terms:
                if term_present(term, all_text):
                    problems.append(f"{tag}: a document contains '{term}', so this question may be answerable")
    for name in golden.documents:
        if name not in texts:
            problems.append(f"document {name} could not be loaded")
    return problems


def summary(golden: Golden) -> str:
    lines = [f"{len(golden.questions)} questions: {len(golden.answerable)} answerable, {len(golden.unanswerable)} unanswerable, "
             f"{len(golden.documents)} documents"]
    by = lambda items, key: ", ".join(f"{k} {n}" for k, n in sorted(Counter(key(i) for i in items).items()))
    lines.append("answerable by type:       " + by(golden.answerable, lambda q: q.type))
    lines.append("answerable by difficulty: " + by(golden.answerable, lambda q: q.difficulty))
    lines.append("unanswerable by type:     " + by(golden.unanswerable, lambda q: q.type))
    lines.append("by origin:                " + by(golden.questions, lambda q: q.origin) + "   (v1 = Phase 1-3 question, v2 = new)")
    return "\n".join(lines)


# ---------------- how hard is it? (retrieval + gate, per question type) ----------------
def group_ranks(hits, q: Question) -> list[int | None]:
    """For every source group: the rank of the first retrieved chunk that satisfies it (None = not retrieved)."""
    out = []
    for group in q.sources:
        rank = next((i for i, h in enumerate(hits, 1)
                     if any(h.file_name == f and contains(h.content, p) for f, p in group)), None)
        out.append(rank)
    return out


def retrieval_report(golden: Golden) -> dict:
    """Index all documents in a throwaway database, run every question through search + rerank + gate."""
    from indexing import search_eval
    from indexing.confidence import decide
    from indexing.search import search

    paths = corpus_paths(golden.documents)
    rows = []
    with search_eval.temp_database():
        search_eval.index_golden_documents(golden.documents, resolve=lambda n: paths[n])
        for q in golden.questions:
            hits = search(q.q, k=20, rerank=True)
            d = decide(hits[:5])
            row = {"id": q.id, "type": q.type, "difficulty": q.difficulty, "origin": q.origin, "answerable": q.answerable,
                   "gate_passes": d.answerable, "top_rerank": d.top_rerank, "top_vector": d.top_vector}
            if q.answerable:
                ranks = group_ranks(hits, q)
                done = None if None in ranks else max(ranks)            # the rank at which ALL the evidence has been retrieved
                row.update(rank=done, ranks=ranks)
            rows.append(row)
    return {"rows": rows}


def _metrics(rows: list[dict]) -> dict:
    n = len(rows)
    ranks = [r["rank"] for r in rows]
    return {"n": n, "r@1": sum(1 for x in ranks if x == 1) / n, "r@5": sum(1 for x in ranks if x and x <= 5) / n,
            "mrr": sum(1 / x for x in ranks if x) / n, "gate_blocked": sum(not r["gate_passes"] for r in rows) / n}


def print_retrieval(report: dict) -> None:
    rows = report["rows"]
    ans = [r for r in rows if r["answerable"]]
    print(f"\nRETRIEVAL on answerable questions (hybrid + rerank; a multi-chunk question counts as found only when ALL its evidence is):")
    print(f"{'GROUP':18}{'N':>4}{'R@1':>8}{'R@5':>8}{'MRR':>8}{'gate blocks':>13}")

    def line(label, subset):
        if subset:
            m = _metrics(subset)
            print(f"{label:18}{m['n']:>4}{m['r@1']:>8.2f}{m['r@5']:>8.2f}{m['mrr']:>8.2f}{m['gate_blocked']:>12.0%}")

    line("ALL", ans)
    for origin in ("v1", "v2"):
        line(f"origin {origin}", [r for r in ans if r["origin"] == origin])
    for d in ("easy", "medium", "hard"):
        line(f"difficulty {d}", [r for r in ans if r["difficulty"] == d])
    for t in sorted({r["type"] for r in ans}):
        line(f"type {t}", [r for r in ans if r["type"] == t])
    missed = [r for r in ans if r["rank"] is None or r["rank"] > 5]
    print(f"\nAnswerable questions whose evidence is NOT all in the top 5 ({len(missed)}):")
    for r in missed:
        print(f"  {r['id']} [{r['type']}] ranks per source group: {r['ranks']}")
    blocked = [r for r in ans if not r["gate_passes"]]
    print(f"\nAnswerable questions the 'I don't know' gate would BLOCK ({len(blocked)}):")
    for r in blocked:
        print(f"  {r['id']} [{r['type']}] rerank {r['top_rerank']:.1f}, meaning {r['top_vector']:.2f}")
    un = [r for r in rows if not r["answerable"]]
    for t in ("off_topic", "near_topic"):
        sub = [r for r in un if r["type"] == t]
        print(f"{t}: the gate blocks {sum(not r['gate_passes'] for r in sub)} of {len(sub)}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.answer_golden")
    ap.add_argument("--retrieval", action="store_true", help="also measure retrieval and the gate per question type")
    args = ap.parse_args(argv)
    golden = load_golden()
    print(summary(golden))
    problems = validate(golden, corpus_texts(golden))
    print(f"\nvalidation: {'OK, no problems' if not problems else str(len(problems)) + ' PROBLEMS'}")
    for p in problems:
        print("  -", p)
    if args.retrieval and not problems:
        print_retrieval(retrieval_report(golden))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
