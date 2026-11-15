"""
Rule-based scoring of answers (Phase 4 Stage 2): fixed rules, no LLM, same input -> same score, every time.

A RECORD is what the runner saved for one question (see answer_run.py):
    id, status, text, model, latency_ms,
    context   [{file, heading, chunk_id, content}]   the excerpts the LLM was given, in order
    citations [{n, chunk_id}]                          the sources the answer cited (already validated)
    gate      {answerable, top_rerank, top_vector}

Because scoring only needs the saved records and the answer key, answers can be RE-SCORED at any time (new rules,
the LLM judge in Stage 3) without calling the LLM again.

What is measured
  answerable questions
    correct             answered AND every required fact is in the answer AND no look-alike wrong value is stated
    lookalike_mention   every fact is there, but it also states a wrong value from a look-alike document
    incorrect           answered, but some required fact is missing
    refused             the assistant said "not found" (gate / LLM / citation check) although the documents have the answer
  unanswerable questions
    refused_ok          the assistant said "not found"
    soft_refusal        it answered, but the text itself says the documents do not contain it (a "hedge")
    false_answer        it answered: the dangerous failure, a made-up answer
  WHY a failure happened (cause), by looking at what the LLM was given:
    retrieval   the evidence was NOT in the excerpts, so no LLM could have answered
    generation  the evidence WAS in the excerpts and the answer is still wrong or missing
    gate        the "I don't know" score gate blocked the question
  citations: precision = share of cited sources that really are evidence; recall = share of evidence groups that were cited

Limits, by design: fact matching cannot judge wording, completeness beyond the listed facts, or an extra wrong claim
that is not on the wrong_facts list. That is what the LLM judge (Stage 3) is for.
"""
import math
import re
from collections import Counter, defaultdict

from indexing.answer_golden import Golden, Question, contains, fact_present, fact_recall
from indexing.retrieval_eval import norm

# ---------------- text helpers ----------------
_HEDGES = [
    r"\b(do|does|did|could|can|will)\s*(not|n't)\s+(\w+\s+){0,3}(state|specify|specified|mention|mentioned|provide|provided|contain|say|cover|covered|include|included|give|indicate|address)\b",
    r"\b(not|n't)\s+(explicitly\s+)?(stated|specified|mentioned|provided|available|covered|included|addressed|clear|found)\b",
    r"\bno\s+(specific\s+|explicit\s+)?(information|details?|mention|data)\b",
    r"\b(cannot|can't|unable to)\s+(find|determine|tell|confirm)\b",
    r"\b(unclear|unspecified)\b",
    r"\b(excerpts?|documents?|policy|text)\s+(do|does)\s*(not|n't)\b",
]
_HEDGE_RE = re.compile("|".join(_HEDGES), re.IGNORECASE)


def strip_citations(text: str) -> str:
    return re.sub(r"\[\d+\]", "", text)


def is_hedged(text: str) -> bool:
    """The answer itself says the documents do not contain (all of) it: 'the excerpts do not state X'."""
    return bool(_HEDGE_RE.search(strip_citations(text)))


def token_f1(answer: str, reference: str) -> float:
    """Word overlap between the answer and the reference answer (0-1). A rough, secondary signal: paraphrases score low."""
    a, r = norm(strip_citations(answer)).split(), norm(reference).split()
    if not a or not r:
        return 0.0
    common = sum((Counter(a) & Counter(r)).values())
    if common == 0:
        return 0.0
    p, rc = common / len(a), common / len(r)
    return 2 * p * rc / (p + rc)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a proportion k/n (better than the usual formula for small n and for rates near 0 or 1)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n if n else None), "ci": [round(lo, 3), round(hi, 3)]}


# ---------------- scoring one record ----------------
def evidence_groups(chunks: list[dict], q: Question) -> list[bool]:
    """For each source group: does ANY of the given chunks satisfy it (right file AND contains the phrase)?"""
    return [any(c["file"] == f and contains(c["content"], p) for c in chunks for f, p in group) for group in q.sources]


def _cited_chunks(rec: dict) -> list[dict]:
    by_id = {c["chunk_id"]: c for c in rec.get("context", [])}
    out = []
    for c in rec.get("citations", []):
        chunk = by_id.get(c.get("chunk_id"))
        if chunk is None and 1 <= c.get("n", 0) <= len(rec.get("context", [])):
            chunk = rec["context"][c["n"] - 1]
        if chunk is not None:
            out.append(chunk)
    return out


def score_record(q: Question, rec: dict) -> dict:
    status = rec["status"]
    answered = status == "answered"
    text = rec.get("text", "") if answered else ""
    out = {"id": q.id, "type": q.type, "difficulty": q.difficulty, "origin": q.origin, "answerable": q.answerable,
           "status": status, "answered": answered, "latency_ms": rec.get("latency_ms") or None,
           "hedged": is_hedged(text) if answered else None}

    if not q.answerable:
        if answered:
            out.update(outcome="soft_refusal" if out["hedged"] else "false_answer", cause="generation")
        elif status in ("llm_unavailable", "error"):
            out.update(outcome="unavailable", cause="llm")
        else:
            out.update(outcome="refused_ok", cause="gate" if status == "refused_gate" else "llm")
        return out

    groups = evidence_groups(rec.get("context", []), q)
    in_context = all(groups)
    cited = _cited_chunks(rec)
    cited_groups = evidence_groups(cited, q) if answered else []
    out.update(evidence_in_context=in_context, evidence_groups=f"{sum(groups)}/{len(groups)}")

    if answered:
        recall = fact_recall(q.facts, text)
        wrong = [w for w in q.wrong_facts if fact_present(w, text)]
        useful = sum(any(c["file"] == f and contains(c["content"], p) for g in q.sources for f, p in g) for c in cited)
        out.update(fact_recall=recall, facts_missing=[f for f in q.facts if not fact_present(f, text)], wrong_fact_hits=wrong,
                   citation_recall=sum(cited_groups) / len(cited_groups) if cited_groups else None,
                   citation_precision=(useful / len(cited)) if cited else None,
                   token_f1=token_f1(text, q.reference))
        out["cites_right"] = out["citation_recall"] == 1.0
        if recall == 1.0 and not wrong:
            out.update(outcome="correct", cause="-")
        elif recall == 1.0:
            out.update(outcome="lookalike_mention", cause="generation")
        else:
            out.update(outcome="incorrect", cause="generation" if in_context else "retrieval")
    elif status == "refused_gate":
        out.update(outcome="refused", cause="gate")
    elif status in ("llm_unavailable", "error"):
        out.update(outcome="unavailable", cause="llm")
    else:                                       # refused_llm, uncited
        out.update(outcome="uncited" if status == "uncited" else "refused", cause="generation" if in_context else "retrieval")
    return out


def score_all(golden: Golden, records: list[dict]) -> list[dict]:
    by_id = {r["id"]: r for r in records}
    return [score_record(q, by_id[q.id]) for q in golden.questions if q.id in by_id]


# ---------------- the summary ----------------
def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def summarize(scored: list[dict]) -> dict:
    ans = [s for s in scored if s["answerable"]]
    un = [s for s in scored if not s["answerable"]]
    n_a, n_u = len(ans), len(un)
    answered = [s for s in ans if s["answered"]]

    def count(items, outcome):
        return sum(s["outcome"] == outcome for s in items)

    summary = {
        "n": len(scored), "n_answerable": n_a, "n_unanswerable": n_u,
        "unavailable": sum(s["outcome"] == "unavailable" for s in scored),      # the LLM never answered: neither right nor wrong
        "answerable": {
            "accuracy": rate(count(ans, "correct"), n_a),
            "all_facts_present": rate(count(ans, "correct") + count(ans, "lookalike_mention"), n_a),
            "answered": rate(len(answered), n_a),
            "incorrect": rate(count(ans, "incorrect"), n_a),
            "refused": rate(count(ans, "refused") + count(ans, "uncited"), n_a),
            "fact_recall_when_answered": _mean(s["fact_recall"] for s in answered),
            "cites_right_when_answered": rate(sum(bool(s.get("cites_right")) for s in answered), len(answered)),
            "citation_precision_mean": _mean(s["citation_precision"] for s in answered),
            "hedged_when_answered": rate(sum(bool(s["hedged"]) for s in answered), len(answered)),
            "lookalike_mentions": count(ans, "lookalike_mention"),
            "token_f1_mean": _mean(s["token_f1"] for s in answered),
        },
        "unanswerable": {
            "refused_ok": rate(count(un, "refused_ok"), n_u),
            "false_answer": rate(count(un, "false_answer"), n_u),
            "soft_refusal": rate(count(un, "soft_refusal"), n_u),
            "by_type": {t: {"refused_ok": rate(count([s for s in un if s["type"] == t], "refused_ok"), sum(s["type"] == t for s in un)),
                            "refused_by_gate": sum(1 for s in un if s["type"] == t and s["cause"] == "gate" and s["outcome"] == "refused_ok")}
                        for t in sorted({s["type"] for s in un})},
        },
        "confusion": {
            "should_answer": {"answered": len(answered), "refused": sum(s["outcome"] in ("refused", "uncited") for s in ans),
                              "unavailable": count(ans, "unavailable")},
            "should_refuse": {"answered": sum(s["answered"] for s in un), "refused": count(un, "refused_ok"),
                              "unavailable": count(un, "unavailable")},
        },
        "why_not_correct": dict(Counter(f"{s['outcome']} / {s['cause']}" for s in ans if s["outcome"] != "correct")),
        "evidence_in_context": rate(sum(bool(s["evidence_in_context"]) for s in ans), n_a),
    }
    for key in ("type", "difficulty", "origin"):
        groups = defaultdict(list)
        for s in ans:
            groups[s[key]].append(s)
        summary[f"by_{key}"] = {g: {"n": len(v), "accuracy": rate(count(v, "correct"), len(v)),
                                    "evidence_in_context": rate(sum(bool(s["evidence_in_context"]) for s in v), len(v))}
                                for g, v in sorted(groups.items())}
    latencies = sorted(s["latency_ms"] for s in scored if s["latency_ms"])
    summary["llm_latency_ms"] = ({"median": latencies[len(latencies) // 2], "p95": latencies[int(len(latencies) * 0.95) - 1], "n": len(latencies)}
                                 if latencies else None)
    return summary



def fmt(r: dict) -> str:
    """'71%  (CI 62-79%, 66/93)'"""
    if r["rate"] is None:
        return "n/a"
    return f"{r['rate']:.0%}  (95% CI {r['ci'][0]:.0%}-{r['ci'][1]:.0%}, {r['k']}/{r['n']})"


def format_report(summary: dict, scored: list[dict], golden: Golden) -> str:
    a, u, c = summary["answerable"], summary["unanswerable"], summary["confusion"]
    q_by_id = {q.id: q for q in golden.questions}
    L = [f"\n{summary['n']} questions: {summary['n_answerable']} answerable, {summary['n_unanswerable']} unanswerable\n"]
    if summary.get("unavailable"):
        L += [f"*** WARNING: the LLM never answered {summary['unavailable']} questions (a rate limit or a daily quota?).",
              "*** This run is INCOMPLETE: those questions are counted neither as right nor as wrong, so every rate below is for a partial exam.",
              "*** Run the same command again later: answers already received are cached, only the missing ones are asked.\n"]
    L += [
         "ANSWERABLE QUESTIONS (rule-based)",
         f"  correct (all facts, no look-alike value)    {fmt(a['accuracy'])}",
         f"  all required facts present                  {fmt(a['all_facts_present'])}",
         f"  answered / wrongly refused                  {fmt(a['answered'])}  /  {fmt(a['refused'])}",
         f"  incorrect (answered, facts missing)         {fmt(a['incorrect'])}",
         f"  evidence was in the excerpts                {fmt(summary['evidence_in_context'])}",
         f"  fact recall when answered (mean)            {a['fact_recall_when_answered']}",
         f"  cites the right evidence when answered      {fmt(a['cites_right_when_answered'])}",
         f"  citation precision (mean)                   {a['citation_precision_mean']}",
         f"  hedged answers ('the excerpts do not state')  {fmt(a['hedged_when_answered'])}",
         f"  look-alike value stated alongside the fact  {a['lookalike_mentions']}",
         f"  word overlap with the reference (mean F1)   {a['token_f1_mean']}   (rough: paraphrases score low)",
         "", "UNANSWERABLE QUESTIONS",
         f"  refused (correct)                           {fmt(u['refused_ok'])}",
         f"  FALSE ANSWER (made up)                      {fmt(u['false_answer'])}",
         f"  soft refusal (answered, but hedged)         {fmt(u['soft_refusal'])}"]
    for t, d in u["by_type"].items():
        L.append(f"    {t:12} refused {fmt(d['refused_ok'])}; {d['refused_by_gate']} of them by the score gate")
    L += ["", "CONFUSION (rows = what should happen)",
          f"  should answer ({summary['n_answerable']:3}):  answered {c['should_answer']['answered']:3}   refused {c['should_answer']['refused']:3}   unavailable {c['should_answer']['unavailable']}",
          f"  should refuse ({summary['n_unanswerable']:3}):  answered {c['should_refuse']['answered']:3}   refused {c['should_refuse']['refused']:3}   unavailable {c['should_refuse']['unavailable']}",
          "", "WHY ANSWERABLE QUESTIONS WERE NOT CORRECT (outcome / cause)"]
    L += [f"  {n:3}  {k}" for k, n in sorted(summary["why_not_correct"].items(), key=lambda kv: -kv[1])] or ["  (none)"]
    for key, title in (("by_type", "TYPE"), ("by_difficulty", "DIFFICULTY"), ("by_origin", "ORIGIN")):
        L += ["", f"ACCURACY BY {title}"]
        for g, d in summary[key].items():
            L.append(f"  {g:14} n={d['n']:3}  correct {fmt(d['accuracy']):38} evidence in excerpts {d['evidence_in_context']['rate']:.0%}")
    if summary["llm_latency_ms"]:
        L += ["", f"LLM time per answered question: median {summary['llm_latency_ms']['median']} ms, p95 {summary['llm_latency_ms']['p95']} ms"]
    bad = [s for s in scored if s["answerable"] and s["outcome"] != "correct"]
    L += ["", f"ANSWERABLE QUESTIONS NOT CORRECT ({len(bad)}):"]
    for s in bad:
        extra = f" missing {s['facts_missing']}" if s.get("facts_missing") else ""
        extra += f" wrong {s['wrong_fact_hits']}" if s.get("wrong_fact_hits") else ""
        L.append(f"  {s['id']} [{s['type']}/{s['difficulty']}] {s['outcome']} / {s['cause']}{extra} :: {q_by_id[s['id']].q[:70]}")
    worse = [s for s in scored if not s["answerable"] and s["outcome"] in ("false_answer", "soft_refusal")]
    L += ["", f"UNANSWERABLE QUESTIONS THAT WERE ANSWERED ({len(worse)}):"]
    for s in worse:
        L.append(f"  {s['id']} [{s['type']}] {s['outcome']} :: {q_by_id[s['id']].q[:80]}")
    return "\n".join(L)
