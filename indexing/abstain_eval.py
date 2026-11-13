"""
"I don't know" evaluation: does the gate answer what it should, and refuse what it should?

Three groups of questions, run through the real pipeline (hybrid search -> reranker -> gate):
  answerable   the 44 questions whose answer IS in the documents   -> the gate must let them through
  off_topic    10 questions unrelated to the company              -> must be blocked
  near_topic   22 questions that sound like our documents but whose answer is NOT there
               -> the score gate can only catch a few; the LLM step (Stage 4) must refuse the rest

It also prints the MARGIN: how far the thresholds sit from the nearest answerable and the nearest
no-answer question. A small margin means the thresholds are fragile. Re-run this after changing the
embedding model, the reranker or the document set.

  python -m indexing.abstain_eval                 compare with the saved baseline
  python -m indexing.abstain_eval --save-baseline
Everything runs in a temporary database; your data is never touched.
"""
import argparse
import json
import sys

from indexing import search_eval
from indexing.retrieve import retrieve
from ingestion.config import PROJECT_ROOT, settings

UNANSWERABLE = PROJECT_ROOT / "evals" / "unanswerable.json"
BASELINE = PROJECT_ROOT / "evals" / "abstain_baseline.json"
LATEST = PROJECT_ROOT / "evals" / "abstain_latest.json"


def load_questions() -> dict[str, list[str]]:
    un = json.loads(UNANSWERABLE.read_text(encoding="utf-8"))
    answerable = []
    for path in search_eval.SETS.values():
        answerable += [q["q"] for q in json.loads(path.read_text(encoding="utf-8"))["questions"]]
    return {"answerable": answerable, "off_topic": un["off_topic"], "near_topic": un["near_topic"]}


def run() -> dict:
    questions = load_questions()
    docs = list(dict.fromkeys(d for p in search_eval.SETS.values()
                              for d in json.loads(p.read_text(encoding="utf-8"))["documents"]))
    results = {}
    with search_eval.temp_database():
        search_eval.index_golden_documents(docs)
        for group, qs in questions.items():
            rows = []
            for q in qs:
                r = retrieve(q)
                rows.append({"q": q, "answered": r.decision.answerable, "code": r.decision.code,
                             "top_rerank": r.decision.top_rerank, "top_vector": r.decision.top_vector,
                             "top": f"{r.hits[0].file_name} > {r.hits[0].heading_path.split(' > ')[-1]}" if r.hits else ""})
            results[group] = rows
    n = {g: len(rows) for g, rows in results.items()}
    summary = {
        "answerable_answered": sum(r["answered"] for r in results["answerable"]) / n["answerable"],
        "off_topic_blocked": sum(not r["answered"] for r in results["off_topic"]) / n["off_topic"],
        "near_topic_blocked": sum(not r["answered"] for r in results["near_topic"]) / n["near_topic"],
    }
    answerable, others = results["answerable"], results["off_topic"] + results["near_topic"]
    margin = {
        "weakest_answerable_rerank": min(r["top_rerank"] for r in answerable),
        "strongest_off_topic_rerank": max(r["top_rerank"] for r in results["off_topic"]),
        "weakest_answerable_vector": min(r["top_vector"] for r in answerable),
        "strongest_off_topic_vector": max(r["top_vector"] for r in results["off_topic"]),
    }
    return {"thresholds": {"min_rerank_score": settings.min_rerank_score, "min_vector_score": settings.min_vector_score},
            "counts": n, "summary": summary, "margin": margin, "groups": results}


def print_report(res: dict, baseline: dict | None) -> None:
    s, n, m = res["summary"], res["counts"], res["margin"]
    print(f"\nthresholds: block if rerank < {res['thresholds']['min_rerank_score']} or meaning < {res['thresholds']['min_vector_score']}\n")
    base = (baseline or {}).get("summary", {})

    def line(label, key, total, want):
        d = "" if key not in base else ("   (same as baseline)" if abs(s[key] - base[key]) < 1e-9
                                         else f"   (baseline {base[key]:.0%})")
        print(f"  {label:42}{s[key]:6.0%}  of {total:<3} {want}{d}")

    line("answerable questions still answered", "answerable_answered", n["answerable"], "(want 100%)")
    line("off-topic questions blocked", "off_topic_blocked", n["off_topic"], "(want 100%)")
    line("near-topic questions blocked by scores alone", "near_topic_blocked", n["near_topic"], "(the LLM must refuse the rest)")
    print(f"\nmargin   rerank: weakest answerable {m['weakest_answerable_rerank']:.1f}  |  threshold {res['thresholds']['min_rerank_score']}"
          f"  |  strongest off-topic {m['strongest_off_topic_rerank']:.1f}")
    print(f"         meaning: weakest answerable {m['weakest_answerable_vector']:.2f}  |  threshold {res['thresholds']['min_vector_score']}"
          f"  |  strongest off-topic {m['strongest_off_topic_vector']:.2f}")
    wrong = [r for r in res["groups"]["answerable"] if not r["answered"]]
    leaked = [r for r in res["groups"]["off_topic"] if r["answered"]]
    for r in wrong:
        print(f"\n  WRONGLY BLOCKED: {r['q']}  (rerank {r['top_rerank']:.1f}, meaning {r['top_vector']:.2f})")
    for r in leaked:
        print(f"\n  OFF-TOPIC LET THROUGH: {r['q']}  (rerank {r['top_rerank']:.1f}, meaning {r['top_vector']:.2f})")
    passed = [r for r in res["groups"]["near_topic"] if r["answered"]]
    print(f"\nNear-topic questions that pass the gate ({len(passed)}) - Stage 4's LLM must say 'not in the documents':")
    for r in passed:
        print(f"  rerank {r['top_rerank']:6.1f}  meaning {r['top_vector']:.2f}  {r['q']}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.abstain_eval")
    ap.add_argument("--save-baseline", action="store_true")
    args = ap.parse_args(argv)
    res = run()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    print_report(res, baseline)
    LATEST.write_text(json.dumps(res, indent=2), encoding="utf-8")
    if args.save_baseline:
        BASELINE.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"\nBaseline saved: {BASELINE.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
