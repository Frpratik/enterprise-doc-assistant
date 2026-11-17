"""
The regression gate (Phase 4 Stage 5): ONE command that says whether a change made the assistant worse.

  python -m indexing.eval_gate                       PASS / FAIL / INCOMPLETE, needs no API key and no quota
  python -m indexing.eval_gate --live                ask the real LLM for questions whose prompt changed (uses quota, replies are cached)
  python -m indexing.eval_gate --skip-access         do not run the access exam (the verdict can then be at best INCOMPLETE)
  python -m indexing.eval_gate --skip-safety         do not run the safety exam (the verdict can then be at best INCOMPLETE)
  python -m indexing.eval_gate --record --from RUN1 RUN2 RUN3     (re)create the baseline from saved runs of the same model
  exit code:  0 PASS     1 FAIL (a regression)     2 INCOMPLETE (something could not be verified)

How it works, and why it looks like this (every rule comes from a measurement in Phase 4):
  * A STRICT rule, not a percentage. The exam can only certify a drop of 6 or more questions (Stage 4), but "a question that was ALWAYS right
    in the baseline is now wrong" fired on a real 4-point regression and gave 0 false alarms for the main model.
  * RECORD / REPLAY. The baseline stores the exact LLM reply for every prompt of the baseline run. The gate re-runs everything EXCEPT the LLM
    (search, reranker, the "I don't know" score gate, prompt building, citation checks, scoring) and replays the stored replies. So it needs no
    key and no quota (the main model allows only ~300 answers a day) and it can run in CI.
  * If a change alters what the LLM would SEE, the stored reply no longer matches that prompt. The gate then says so (UNVERIFIED) instead of
    pretending: verify with --live, or re-record the baseline once the change is confirmed to be intended (like updating a snapshot test).
  * SILENCE IS NOT SUCCESS. A question that cannot be verified makes the verdict INCOMPLETE, never PASS (an LLM quota once looked like a model changing its mind).

What makes the verdict FAIL (a regression):
  new_failure          a question that was correct in EVERY baseline run is not correct now
  lost_evidence        the excerpts no longer contain the evidence the baseline had (a retrieval regression: needs no LLM to see)
  gate_blocks_answerable   the score gate now blocks a question the documents can answer
  gate_no_longer_blocks    the score gate no longer blocks an unanswerable question it used to block (a safety layer got weaker)
  false_answer         an unanswerable question got an answer (or a hedged answer)
  answer_key_changed   the baseline was recorded with another version of the answer key
  forbidden_evidence   (Phase 5) the access exam found something a person may not read in a prompt, answer, citation or stream event. Zero tolerance, no baseline
  access_over_block    (Phase 5) a person who may read the evidence did not get it, although an all-roles auditor did
  access_exam_vacuous  (Phase 5) the access exam withheld or served nothing, so it proved nothing
  The access exam (indexing/access_exam.py) needs no LLM, key or quota; it is skipped for a quick --ids check and is INCOMPLETE when skipped on purpose.
  new_attack_success   (Phase 6) a live attack that was held when the safety baseline was recorded now succeeds (a forbidden string reached the person)
  ingestion_regression / pii_leak / pii / output_leak / output_guard / abuse / prompt_exposure / prompt_structure / attack_error   (Phase 6) the safety exam's own checks,
                       zero tolerance and no baseline. A replayed attack whose prompt changed is UNVERIFIED (INCOMPLETE), never a silent pass.
  The safety exam (indexing/safety_exam.py) replays the stored replies of the real LLM: no key, no quota.
What is only REPORTED: questions that were wrong in the baseline and are right now (improvements: re-record to lock them in).
The LLM judge (faithfulness, correctness) is NOT part of the gate: it is slow and limited to ~8 calls a minute. It is a periodic audit.
"""
import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from indexing import access_exam, answer_run, safety_exam
from indexing.answer_golden import Golden, load_golden
from indexing.answer_score import evidence_groups, score_all
from indexing.answer_stats import reliability, unavailable_ids
from indexing.llm import LLMResult, LLMUnavailable
from ingestion.config import PROJECT_ROOT, settings

GATE_BASELINE = PROJECT_ROOT / "evals" / "gate_baseline.json"
EXIT = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}


# ---------------- record / replay of LLM replies ----------------
def prompt_key(messages: list[dict]) -> str:
    """The identity of a prompt: the same excerpts in the same order for the same question give the same key."""
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def question_of(messages: list[dict]) -> str:
    """The question text inside a prompt (it comes last)."""
    return messages[-1]["content"].rsplit("Question: ", 1)[1].strip()


class ReplayMiss(LLMUnavailable):
    """The prompt has no stored reply: it is not the prompt of the baseline run."""


class ReplayLLM:
    """Answers from the stored replies. A prompt that is not in the baseline is a MISS (it is remembered, per question)."""

    def __init__(self, replies: dict):
        self.replies, self.missed_questions = replies, set()

    def chat(self, messages, tier="main", **kw):
        reply = self.replies.get(prompt_key(messages))
        if reply is None:
            self.missed_questions.add(question_of(messages))
            raise ReplayMiss([])
        return LLMResult(reply["text"], reply["model"], tier, [], {}, 0)


class LiveFallbackLLM(ReplayLLM):
    """Replay what is stored; for a prompt that changed, ask the real LLM (replies are cached on disk)."""

    def __init__(self, replies: dict, live):
        super().__init__(replies)
        self.live, self.asked_live = live, set()

    def chat(self, messages, tier="main", **kw):
        if prompt_key(messages) in self.replies:
            return super().chat(messages, tier=tier, **kw)
        self.asked_live.add(question_of(messages))
        return self.live.chat(messages, tier=tier, **kw)


class RecordingLLM:
    """Wraps a real (usually cached) LLM client and remembers every reply by prompt key."""

    def __init__(self, inner):
        self.inner, self.replies = inner, {}

    @property
    def last_ms(self):
        return getattr(self.inner, "last_ms", 0)

    def chat(self, messages, tier="main", **kw):
        res = self.inner.chat(messages, tier=tier, **kw)
        self.replies[prompt_key(messages)] = {"text": res.text, "model": res.model}
        return res


# ---------------- the verdict (pure, testable) ----------------
@dataclass
class Gate:
    verdict: str = "PASS"
    failures: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT[self.verdict]


NOT_RUN = object()          # evaluate(): the access exam is not part of this call (pure unit tests of the answer rules)


def evaluate(baseline: dict, golden: Golden, scored: list[dict], records: list[dict], missed_questions: set[str], access=NOT_RUN, safety=NOT_RUN) -> Gate:
    """Compare a run with the baseline. `missed_questions` = questions whose prompt had no stored reply (replay misses).
    `access`: the ExamResult of the access exam, or None = it was not run (then the verdict is at best INCOMPLETE), or NOT_RUN = not part of this call.
    `safety`: the SafetyResult of the safety exam, with the same three meanings."""
    gate = Gate()
    q_by_id = {q.id: q for q in golden.questions}
    sc = {s["id"]: s for s in scored}
    rec = {r["id"]: r for r in records}
    missed = {i for i, q in q_by_id.items() if q.q in missed_questions}
    unavailable = unavailable_ids(scored)

    def fail(check, qid, detail=""):
        gate.failures.append({"check": check, "id": qid, "detail": detail, "question": q_by_id[qid].q if qid in q_by_id else ""})

    def unverified(qid, why):
        gate.unverified.append({"id": qid, "why": why, "question": q_by_id[qid].q})

    if baseline["meta"]["golden_version"] != golden.version:
        gate.failures.append({"check": "answer_key_changed", "id": None, "question": "",
                              "detail": f"the baseline was recorded with answer key v{baseline['meta']['golden_version']}, the key is now v{golden.version}: re-record the baseline"})

    for qid in baseline["stable_correct"]:                                         # 1. a question that was ALWAYS right
        if qid not in sc:
            continue
        if qid in missed:
            unverified(qid, "its prompt changed: no stored reply")
        elif qid in unavailable:
            unverified(qid, "the LLM did not answer")
        elif sc[qid]["outcome"] != "correct":
            fail("new_failure", qid, f"{sc[qid]['outcome']} / {sc[qid]['cause']}" + (f", missing {sc[qid]['facts_missing']}" if sc[qid].get("facts_missing") else ""))

    for qid in baseline["evidence_in_context"]:                                    # 2. the evidence must still be given to the LLM
        if qid in rec and not all(evidence_groups(rec[qid]["context"], q_by_id[qid])):
            fail("lost_evidence", qid, f"evidence groups now in the excerpts: {sum(evidence_groups(rec[qid]['context'], q_by_id[qid]))}/{len(q_by_id[qid].sources)}")

    for qid in baseline["answerable_gate_passes"]:                                 # 3. the score gate must not block answerable questions
        if qid in rec and not rec[qid]["gate"]["answerable"]:
            fail("gate_blocks_answerable", qid, f"rerank {rec[qid]['gate']['top_rerank']}, meaning {rec[qid]['gate']['top_vector']}")

    for qid in baseline["gate_blocked_unanswerable"]:                              # 4. the score gate must keep blocking what it blocked
        if qid in rec and rec[qid]["gate"]["answerable"]:
            fail("gate_no_longer_blocks", qid, f"rerank {rec[qid]['gate']['top_rerank']}, meaning {rec[qid]['gate']['top_vector']}")

    for s in scored:                                                               # 5. nothing unanswerable may be answered
        if not s["answerable"]:
            if s["outcome"] in ("false_answer", "soft_refusal"):
                fail("false_answer", s["id"], s["outcome"])
            elif s["id"] in missed:
                unverified(s["id"], "its prompt changed: no stored reply")
            elif s["id"] in unavailable and s["id"] in baseline["unanswerable_refused"]:
                unverified(s["id"], "the LLM did not answer")

    if access is None:                                                             # 6. the access exam: skipped on purpose is not success
        gate.unverified.append({"id": "-", "why": "the access exam was not run", "question": "who may see what"})
    elif access is not NOT_RUN:
        for p in access.problems:
            gate.failures.append({"check": p["check"], "id": p["id"], "detail": p["detail"], "question": q_by_id[p["id"]].q if p["id"] in q_by_id else ""})
        gate.info["access"] = {"checks": access.checks, "served": access.served, "denied": access.denied, "leaks": len(access.leaks)}

    if safety is None:                                                             # 7. the safety exam: skipped on purpose is not success
        gate.unverified.append({"id": "-", "why": "the safety exam was not run", "question": "attacks and abuse"})
    elif safety is not NOT_RUN:
        for p in safety.problems:
            gate.failures.append({"check": p["check"], "id": p["id"], "detail": p["detail"], "question": q_by_id[p["id"]].q if p["id"] in q_by_id else ""})
        for attack_id in safety.unverified:
            gate.unverified.append({"id": attack_id, "why": "its prompt changed: no stored reply of the real LLM", "question": "safety attack"})
        gate.info["safety"] = safety.summary()

    gate.improvements = [qid for qid in baseline["known_failures"] if qid in sc and sc[qid]["outcome"] == "correct"]
    ans = [s for s in scored if s["answerable"]]
    gate.info.update({"questions": len(scored), "answerable_correct": sum(s["outcome"] == "correct" for s in ans), "answerable": len(ans),
                 "baseline_stable_correct": len(baseline["stable_correct"]), "baseline_known_failures": baseline["known_failures"],
                 "unanswerable_refused": sum(s["outcome"] == "refused_ok" for s in scored if not s["answerable"])})
    gate.verdict = "FAIL" if gate.failures else ("INCOMPLETE" if gate.unverified else "PASS")
    return gate


def format_gate(gate: Gate) -> str:
    i = gate.info
    L = [f"\nANSWER-PIPELINE REGRESSION GATE: {gate.verdict}",
         f"  checked {i['questions']} questions: {i['answerable_correct']}/{i['answerable']} answerable correct now "
         f"(baseline: {i['baseline_stable_correct']} were correct in every run; wrong in every run: {i['baseline_known_failures']}); "
         f"{i['unanswerable_refused']} unanswerable refused"]
    if "safety" in i:
        sf = i["safety"]
        L.append(f"  safety exam: {sf['checks']} checks, {sf['attacks']} live attacks, {sf['succeeded']} succeeded (known gap {sf['known_successes']}, new {sf['new_successes']}), {sf['findings']} findings")
    if "access" in i:
        a = i["access"]
        L.append(f"  access exam: {a['checks']} checks, {a['served']} served correctly, {a['denied']} withheld correctly, {a['leaks']} leaks")
    if gate.failures:
        L.append(f"\n  REGRESSIONS ({len(gate.failures)}):")
        L += [f"    [{f['check']}] {f['id'] or '-'}  {f['detail']}  {f['question'][:70]}" for f in gate.failures]
    if gate.unverified:
        L.append(f"\n  COULD NOT BE VERIFIED ({len(gate.unverified)}): a stored reply is missing, the LLM did not answer, or the access exam was not run.")
        L += [f"    {u['id']}  {u['why']}  {u['question'][:70]}" for u in gate.unverified[:12]]
        if len(gate.unverified) > 12:
            L.append(f"    ... and {len(gate.unverified) - 12} more")
        L.append("  -> if the change is intended: run with --live (asks the real LLM, uses quota), then re-record the baseline (--record --from RUN...).")
    if gate.improvements:
        L.append(f"\n  IMPROVEMENTS (wrong in the baseline, right now): {gate.improvements}  -> re-record the baseline to lock them in")
    L.append({"PASS": "\n  PASS: no question that was always right got worse, the evidence is still found, the score gate still holds.",
              "FAIL": "\n  FAIL: a regression. Fix it, or - if the change is intended and the new behaviour is better - re-record the baseline.",
              "INCOMPLETE": "\n  INCOMPLETE: nothing got worse among the questions that could be checked, but some could not be verified."}[gate.verdict])
    return "\n".join(L)


# ---------------- running the gate ----------------
def load_baseline(path: Path | None = None) -> dict:
    path = path or GATE_BASELINE                       # looked up when called, so a changed GATE_BASELINE is honoured
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist: create it with  python -m indexing.eval_gate --record --from RUN1.json RUN2.json RUN3.json")
    return json.loads(path.read_text(encoding="utf-8"))


def run_gate(baseline: dict, golden: Golden, live: bool = False, ids: list[str] | None = None, progress=None, access: bool | None = None, safety: bool | None = None) -> Gate:
    """Re-run the pipeline with the stored LLM replies and compare with the baseline, then run the access exam.
    access=None: run it unless a quick --ids check was asked | True: always | False: skipped on purpose (the verdict is then at best INCOMPLETE)."""
    if live:
        from indexing.answer_eval import CachingLLM
        llm = LiveFallbackLLM(baseline["replies"], CachingLLM(max_wait=70, retries=3))
        model = baseline["meta"]["model"]
    else:
        llm, model = ReplayLLM(baseline["replies"]), None
    records = answer_run.generate_repeats(golden, model, 1, llm=llm, ids=ids, progress=progress, stall_after=None)[0]
    scored = score_all(golden, records)
    if access is None:
        access = not ids
        outcome = access_exam.run_full(progress=progress) if access else NOT_RUN
    else:
        outcome = access_exam.run_full(progress=progress) if access else None
    if safety is None:
        safety = not ids
        safety_outcome = safety_exam.run("live" if live else "replay", progress=progress) if safety else NOT_RUN
    else:
        safety_outcome = safety_exam.run("live" if live else "replay", progress=progress) if safety else None
    return evaluate(baseline, golden, scored, records, llm.missed_questions, access=outcome, safety=safety_outcome)


# ---------------- recording the baseline ----------------
def record_baseline(golden: Golden, run_files: list[str], progress=None) -> dict:
    """
    Build the baseline: which questions are ALWAYS right comes from saved runs of the same model; the stored LLM replies come from a recording pass
    (the pipeline is run again through the cached LLM client, so it costs no quota when the replies are cached).
    """
    from indexing.answer_eval import CachingLLM
    runs = [json.loads(Path(f).read_text(encoding="utf-8")) for f in run_files]
    scored_runs = [score_all(golden, r["records"]) for r in runs]
    if any(unavailable_ids(s) & {x["id"] for x in s if x["answerable"]} for s in scored_runs):
        raise ValueError("a saved run has answerable questions the LLM never answered: the baseline must come from complete runs")
    rel = reliability(scored_runs)
    model = runs[0]["meta"]["model"]

    recorder = RecordingLLM(CachingLLM(max_wait=70, retries=3))
    records = answer_run.generate_repeats(golden, model, 1, llm=recorder, progress=progress)[0]
    scored = score_all(golden, records)
    now_correct = {s["id"] for s in scored if s["answerable"] and s["outcome"] == "correct"}
    lost = [i for i in rel["stable_correct"] if i not in now_correct]
    if lost:
        raise ValueError(f"the recording pass does not reproduce the saved runs: {lost} were always right there and are not now (code or prompt changed since?)")

    q_by_id = {q.id: q for q in golden.questions}
    rec = {r["id"]: r for r in records}
    return {
        "meta": {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model, "golden_version": golden.version,
                 "prompt_version": answer_run.answer_mod.PROMPT_VERSION, "source_runs": [Path(f).name for f in run_files],
                 "answerable": len([q for q in golden.questions if q.answerable]), "unanswerable": len([q for q in golden.questions if not q.answerable]),
                 "min_rerank_score": settings.min_rerank_score, "min_vector_score": settings.min_vector_score,
                 "context_chunks": settings.answer_context_chunks, "rerank_candidates": settings.rerank_candidates,
                 "rrf_keyword_weight": settings.rrf_keyword_weight, "replies": len(recorder.replies)},
        "stable_correct": rel["stable_correct"], "known_failures": rel["stable_wrong"],
        "evidence_in_context": [i for i, r in rec.items() if q_by_id[i].answerable and all(evidence_groups(r["context"], q_by_id[i]))],
        "answerable_gate_passes": [i for i, r in rec.items() if q_by_id[i].answerable and r["gate"]["answerable"]],
        "gate_blocked_unanswerable": [i for i, r in rec.items() if not q_by_id[i].answerable and not r["gate"]["answerable"]],
        "unanswerable_refused": [s["id"] for s in scored if not s["answerable"] and s["outcome"] == "refused_ok"],
        "replies": recorder.replies}


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.eval_gate")
    ap.add_argument("--live", action="store_true", help="ask the real LLM for questions whose prompt changed (uses quota)")
    ap.add_argument("--ids", help="only these question ids (a quick check; the access exam is not run)")
    ap.add_argument("--skip-safety", action="store_true", help="do not run the safety exam (the verdict is then at best INCOMPLETE)")
    ap.add_argument("--skip-access", action="store_true", help="do not run the access exam (the verdict is then at best INCOMPLETE)")
    ap.add_argument("--record", action="store_true", help="(re)create the baseline")
    ap.add_argument("--from", dest="runs", nargs="+", metavar="RUN", help="with --record: saved runs of the same model (use 3 for a reliable baseline)")
    args = ap.parse_args(argv)
    if args.record and not args.runs:                          # a usage mistake is reported as one, before anything else is checked
        ap.error("--record needs --from RUN1.json [RUN2.json ...]")

    from indexing.db import can_connect
    if not can_connect():
        print("Database not reachable: start it with  docker compose up -d")
        return 2
    golden = load_golden()
    if args.record:
        try:
            baseline = record_baseline(golden, args.runs, progress=print)
        except ValueError as e:
            print(f"NOT recorded: {e}")
            return 2
        GATE_BASELINE.write_text(json.dumps(baseline, indent=1, ensure_ascii=False), encoding="utf-8")
        m = baseline["meta"]
        print(f"baseline recorded: {GATE_BASELINE.relative_to(PROJECT_ROOT)}  ({m['model']}, answer key v{m['golden_version']}, {m['replies']} stored replies, "
              f"{len(baseline['stable_correct'])} always-right questions, known failures {baseline['known_failures']})")
        return 0
    try:
        baseline = load_baseline()
    except FileNotFoundError as e:
        print(e)
        return 2
    ids = [i.strip() for i in args.ids.split(",")] if args.ids else None
    try:
        gate = run_gate(baseline, golden, live=args.live, ids=ids, progress=print, access=False if args.skip_access else None, safety=False if args.skip_safety else None)
    except Exception as e:                                    # the gate itself broke: that is never a PASS
        print(f"GATE ERROR: {type(e).__name__}: {e}")
        return 2
    print(format_gate(gate))
    return gate.exit_code


if __name__ == "__main__":
    sys.exit(main())
