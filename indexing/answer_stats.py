"""
Spread and significance (Phase 4 Stage 4): how much do the results move when nothing has changed, and what difference is real?

Two kinds of uncertainty, and both are measured:
  * WHICH QUESTIONS were asked (I wrote 93 answerable ones; another author's 93 would give a slightly different score)
                                -> a confidence interval over questions (Wilson for one run, a bootstrap over questions for several runs)
  * WHAT THE MODEL DID THIS TIME (even at temperature 0 the same prompt can give a different answer)
                                -> the same pipeline run several times: the spread between runs, and how many questions FLIP

Reliability, stricter than an average (an idea from agent benchmarks):
  pass@1   the average accuracy of a single run
  pass^k   the share of questions answered correctly in ALL k runs   (a customer who asks twice and gets two different answers is unhappy)
  pass@k   the share answered correctly in AT LEAST ONE of the k runs
  flaky    questions that are right in some runs and wrong in others: where the noise lives

Comparing two runs or models on the SAME questions (paired), because the questions are the same:
  McNemar exact test on the questions where only one of them was right, and a bootstrap interval for the difference in accuracy.

Noise of a regression gate ("fail if a question that was always correct is now wrong"): measured by leaving one run out at a time.

UNAVAILABLE QUESTIONS: if the LLM never answered a question in some run (a rate limit, a daily quota), that run is INCOMPLETE for that
question. It is neither right nor wrong, so the question is left OUT of every comparison and the report says so loudly. (Treating it as a
changed answer would make a quota look like a model that changed its mind: this happened once, see docs/BUILD_LOG.md.)

  python -m indexing.answer_stats --stability RUN1.json RUN2.json RUN3.json
  python -m indexing.answer_stats --compare RUN_A.json RUN_B.json
"""
import argparse
import json
import random
import re
import sys
from math import comb
from pathlib import Path

from indexing.answer_golden import load_golden
from indexing.answer_score import fmt, rate, score_all

NO_ANSWER = ("unavailable",)                       # outcomes meaning "the LLM never answered"


# ---------------- one run -> a yes/no per answerable question ----------------
def unavailable_ids(scored: list[dict]) -> set[str]:
    return {s["id"] for s in scored if s["outcome"] in NO_ANSWER}


def correct_map(scored: list[dict], skip: set[str] = frozenset()) -> dict[str, bool]:
    return {s["id"]: s["outcome"] == "correct" for s in scored if s["answerable"] and s["id"] not in skip}


def accuracy(vec: dict[str, bool]) -> float:
    return sum(vec.values()) / len(vec) if vec else 0.0


def common_ids(vectors: list[dict[str, bool]]) -> list[str]:
    return sorted(set.intersection(*[set(v) for v in vectors])) if vectors else []


def _skip_for(runs: list[list[dict]]) -> set[str]:
    """Questions that at least one run never got an answer for: they cannot be compared."""
    return set().union(*[unavailable_ids(r) for r in runs]) if runs else set()


# ---------------- several runs of the same system ----------------
def reliability(runs: list[list[dict]]) -> dict:
    """pass@1 / pass^k / pass@k and the flaky questions, from scored runs of the SAME system."""
    skip = _skip_for(runs)
    vecs = [correct_map(r, skip) for r in runs]
    ids, k = common_ids(vecs), len(vecs)
    counts = {i: sum(v[i] for v in vecs) for i in ids}
    n = len(ids)
    return {
        "runs": k, "questions": n, "left_out_unavailable": sorted(i for i in skip if any(s["id"] == i and s["answerable"] for r in runs for s in r)),
        "accuracy_per_run": [round(accuracy({i: v[i] for i in ids}), 4) for v in vecs],
        "pass_at_1_mean": round(sum(counts.values()) / (n * k), 4) if n and k else None,
        "pass_hat_k": rate(sum(c == k for c in counts.values()), n),          # correct in ALL runs
        "pass_at_k": rate(sum(c >= 1 for c in counts.values()), n),            # correct in AT LEAST ONE run
        "stable_correct": [i for i in ids if counts[i] == k],
        "stable_wrong": [i for i in ids if counts[i] == 0],
        "flaky": {i: counts[i] for i in ids if 0 < counts[i] < k}}


def bootstrap_mean_accuracy(runs: list[list[dict]], reps: int = 5000, seed: int = 1) -> dict:
    """
    Mean accuracy over the runs, with a 95% interval that comes from resampling the QUESTIONS (so it includes the 'which
    questions' uncertainty). Each question contributes its own average over the runs, so run-to-run noise is inside it too.
    """
    skip = _skip_for(runs)
    vecs = [correct_map(r, skip) for r in runs]
    ids = common_ids(vecs)
    per_q = [sum(v[i] for v in vecs) / len(vecs) for i in ids]
    rng = random.Random(seed)
    n = len(per_q)
    means = sorted(sum(per_q[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    return {"mean": round(sum(per_q) / n, 4), "ci": [round(means[int(0.025 * reps)], 4), round(means[int(0.975 * reps) - 1], 4)], "questions": n}


def text_stability(record_runs: list[list[dict]]) -> dict:
    """How often did the model write exactly the same answer text in every run? (whitespace and citation markers ignored)"""
    def key(text):
        return re.sub(r"\s+", " ", re.sub(r"\[\d+\]", "", text or "")).strip().lower().replace(" ", " ")
    by_run = [{r["id"]: r for r in run} for run in record_runs]
    skip = {i for b in by_run for i, r in b.items() if r["status"] in ("llm_unavailable", "error")}
    ids = sorted(set.intersection(*[set(b) for b in by_run]) - skip)
    same_text = sum(len({key(b[i].get("text")) for b in by_run}) == 1 for i in ids)
    same_status = sum(len({b[i]["status"] for b in by_run}) == 1 for i in ids)
    return {"questions": len(ids), "identical_text": rate(same_text, len(ids)), "same_status": rate(same_status, len(ids))}


def unanswerable_stability(runs: list[list[dict]]) -> dict:
    """Did any run ever answer a question that has no answer? (the dangerous failure) And did the refusals flip?"""
    skip = _skip_for(runs)
    by_run = [{s["id"]: s for s in run if not s["answerable"] and s["id"] not in skip} for run in runs]
    ids = sorted(set.intersection(*[set(b) for b in by_run])) if by_run else []
    false_answers = sorted({i for i in ids for b in by_run if b[i]["outcome"] in ("false_answer", "soft_refusal")})
    flips = [i for i in ids if len({b[i]["outcome"] == "refused_ok" for b in by_run}) > 1]
    return {"questions": len(ids), "answered_in_any_run": false_answers, "flipped": flips,
            "refused_every_time": rate(len(ids) - len({*false_answers, *flips}), len(ids))}


# ---------------- comparing two runs / models on the same questions ----------------
def mcnemar_exact(only_a: int, only_b: int) -> float:
    """
    Two-sided exact p-value. Only the questions where the two systems DISAGREE carry information: if there is no real difference,
    each such question is equally likely to favour either system (a fair coin).
    """
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def needed_discordant(alpha: float = 0.05) -> int:
    """Fewest one-sided disagreements (the other system never wins a question) that are statistically significant."""
    b = 1
    while mcnemar_exact(b, 0) > alpha:
        b += 1
    return b


def paired_bootstrap(a: dict[str, bool], b: dict[str, bool], reps: int = 5000, seed: int = 1) -> dict:
    """95% interval for accuracy(a) - accuracy(b), resampling the questions (the same questions for both)."""
    ids = common_ids([a, b])
    diffs_each = [int(a[i]) - int(b[i]) for i in ids]
    n = len(ids)
    rng = random.Random(seed)
    diffs = sorted(sum(diffs_each[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    return {"diff": round(sum(diffs_each) / n, 4), "ci": [round(diffs[int(0.025 * reps)], 4), round(diffs[int(0.975 * reps) - 1], 4)]}


def compare(scored_a: list[dict], scored_b: list[dict]) -> dict:
    skip = unavailable_ids(scored_a) | unavailable_ids(scored_b)
    a, b = correct_map(scored_a, skip), correct_map(scored_b, skip)
    ids = common_ids([a, b])
    only_a = [i for i in ids if a[i] and not b[i]]
    only_b = [i for i in ids if b[i] and not a[i]]
    boot = paired_bootstrap(a, b)
    return {"questions": len(ids), "accuracy_a": round(accuracy({i: a[i] for i in ids}), 4), "accuracy_b": round(accuracy({i: b[i] for i in ids}), 4),
            "only_a_correct": only_a, "only_b_correct": only_b, "p_value": round(mcnemar_exact(len(only_a), len(only_b)), 4),
            "diff": boot["diff"], "diff_ci": boot["ci"], "both_wrong": [i for i in ids if not a[i] and not b[i]],
            "left_out_unavailable": sorted(skip)}


# ---------------- regression gate: "no new failure among the questions that used to be always right" ----------------
def new_failures(baseline_runs: list[list[dict]], new_run: list[dict]) -> list[str]:
    """Questions that were correct in EVERY baseline run but are wrong in the new run (a question the LLM never answered is not a failure)."""
    stable = reliability(baseline_runs)["stable_correct"]
    new = correct_map(new_run, unavailable_ids(new_run))
    return [i for i in stable if i in new and not new[i]]


def gate_false_alarms(runs: list[list[dict]]) -> dict:
    """
    How noisy is that gate when NOTHING has changed? Leave one run out at a time: the others are the baseline, the held-out run plays
    the 'new' run. Any 'new failure' it finds is a false alarm, caused only by the model's randomness.
    """
    if len(runs) < 3:
        raise ValueError("need at least 3 runs: 2 to form the baseline and 1 to test")
    alarms = []
    for i in range(len(runs)):
        rest = runs[:i] + runs[i + 1:]
        alarms.append(new_failures(rest, runs[i]))
    return {"held_out_runs": len(runs), "false_alarms_per_run": [len(a) for a in alarms], "questions": sorted({q for a in alarms for q in a}),
            "runs_with_a_false_alarm": sum(1 for a in alarms if a)}


# ---------------- reports ----------------
def stability_report(golden, scored_runs: list[list[dict]], record_runs: list[list[dict]], labels: list[str]) -> dict:
    rel = reliability(scored_runs)
    rep = {"labels": labels, "reliability": rel, "bootstrap": bootstrap_mean_accuracy(scored_runs),
           "text": text_stability(record_runs), "unanswerable": unanswerable_stability(scored_runs),
           "unavailable_per_run": [len(unavailable_ids(r)) for r in scored_runs],
           "detectable": {"discordant_needed": needed_discordant(), "n_answerable": rel["questions"]}}
    if len(scored_runs) >= 3:
        rep["gate_noise"] = gate_false_alarms(scored_runs)
    return rep


def unavailable_warning(per_run: list[int]) -> str:
    if not any(per_run):
        return ""
    return ("\n  *** WARNING: the LLM never answered some questions: " + ", ".join(f"run {j + 1}: {n}" for j, n in enumerate(per_run) if n) +
            ".\n  *** Likely a rate limit or a daily quota. Those runs are INCOMPLETE: the affected questions are left out of every number below.\n"
            "  *** Run the same command again later: answers already received are cached, only the missing ones are asked.\n")


def print_stability(rep: dict, golden) -> None:
    q_by_id = {q.id: q for q in golden.questions}
    rel, boot, txt, un = rep["reliability"], rep["bootstrap"], rep["text"], rep["unanswerable"]
    print(f"\nSTABILITY over {rel['runs']} runs of the same pipeline ({rel['questions']} answerable questions compared)")
    print(unavailable_warning(rep.get("unavailable_per_run", [])), end="")
    print("  accuracy of each run         " + "  ".join(f"{a:.1%}" for a in rel["accuracy_per_run"]) + f"    (range {min(rel['accuracy_per_run']):.1%} to {max(rel['accuracy_per_run']):.1%})")
    print(f"  mean accuracy, 95% interval  {boot['mean']:.1%}  ({boot['ci'][0]:.1%} - {boot['ci'][1]:.1%})    <- the interval covers 'which questions' and run-to-run noise")
    print(f"  pass^{rel['runs']} (right in ALL runs)   {fmt(rel['pass_hat_k'])}")
    print(f"  pass@{rel['runs']} (right in any run)    {fmt(rel['pass_at_k'])}")
    print(f"  identical answer text in every run   {fmt(txt['identical_text'])}      same status (answered / refused): {fmt(txt['same_status'])}")
    print(f"  FLAKY questions (right in some runs, wrong in others): {len(rel['flaky'])}")
    for i, c in rel["flaky"].items():
        print(f"      {i}: correct in {c} of {rel['runs']} runs  {q_by_id[i].q[:70]}")
    print(f"  wrong in every run: {rel['stable_wrong']}")
    print(f"  unanswerable questions ({un['questions']} compared): refused every time {fmt(un['refused_every_time'])};  answered in any run: {un['answered_in_any_run']};  flipped: {un['flipped']}")
    d = rep["detectable"]
    print(f"\nWHAT A PAIRED COMPARISON CAN DETECT with {d['n_answerable']} questions: at least {d['discordant_needed']} questions must change for the worse "
          f"with none changing for the better (about {d['discordant_needed'] / max(d['n_answerable'], 1):.0%} of the exam) before the difference is statistically significant (p < 0.05).")
    if "gate_noise" in rep:
        g = rep["gate_noise"]
        print(f"\nNOISE OF A 'NO NEW FAILURE' GATE (leave one run out, {g['held_out_runs']} times, nothing changed): false alarms per run {g['false_alarms_per_run']}; "
              f"{g['runs_with_a_false_alarm']} of {g['held_out_runs']} runs would have failed the gate {g['questions'] or ''}")


def print_compare(c: dict, name_a: str, name_b: str, golden) -> None:
    q_by_id = {q.id: q for q in golden.questions}
    print(f"\n{name_a}  vs  {name_b}   ({c['questions']} answerable questions, same questions for both)")
    if c["left_out_unavailable"]:
        print(f"  *** WARNING: {len(c['left_out_unavailable'])} questions were never answered by the LLM in one of the runs and are left out: {c['left_out_unavailable'][:8]}...")
    print(f"  accuracy: {c['accuracy_a']:.1%} vs {c['accuracy_b']:.1%}    difference {c['diff']:+.1%}   95% interval ({c['diff_ci'][0]:+.1%} to {c['diff_ci'][1]:+.1%})")
    print(f"  only {name_a} right: {len(c['only_a_correct'])}   only {name_b} right: {len(c['only_b_correct'])}   both wrong: {len(c['both_wrong'])}   McNemar exact p = {c['p_value']}"
          f"   -> {'a real difference' if c['p_value'] < 0.05 else 'NOT statistically different'}")
    for i in c["only_a_correct"]:
        print(f"      only {name_a} right: {i} {q_by_id[i].q[:70]}")
    for i in c["only_b_correct"]:
        print(f"      only {name_b} right: {i} {q_by_id[i].q[:70]}")


# ---------------- command line ----------------
def _load(path: str, golden):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["meta"], data["records"], score_all(golden, data["records"])


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.answer_stats")
    ap.add_argument("--stability", nargs="+", metavar="RUN", help="saved runs of the SAME system: spread, reliability, gate noise")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="two saved runs (e.g. two models): paired comparison")
    args = ap.parse_args(argv)
    golden = load_golden()
    if args.stability:
        loaded = [_load(p, golden) for p in args.stability]
        print_stability(stability_report(golden, [s for _, _, s in loaded], [r for _, r, _ in loaded], [m.get("model", "?") for m, _, _ in loaded]), golden)
    if args.compare:
        (ma, _, sa), (mb, _, sb) = (_load(p, golden) for p in args.compare)
        print_compare(compare(sa, sb), ma.get("model", "A"), mb.get("model", "B"), golden)
    if not args.stability and not args.compare:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
