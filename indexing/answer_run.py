"""
Run the REAL answer pipeline on the whole answer key and score the answers with fixed rules.

  python -m indexing.answer_run                       main model (qwen), all 144 questions, report
  python -m indexing.answer_run --model openai/gpt-oss-120b
  python -m indexing.answer_run --ids A001,A002,U001  just these questions
  python -m indexing.answer_run --rescore evals/runs/latest.json     score saved answers again, NO LLM call
  python -m indexing.answer_run --save-baseline       keep this run (records + scores) as evals/answers_v2_baseline.json

How it works
  1. the 22 documents are indexed into a THROWAWAY database (your real data is never touched)
  2. every question goes through retrieve -> gate -> LLM -> citation checks (indexing.answer.answer)
  3. one RECORD per question is saved with everything needed to judge it later: the answer, which excerpts the
     LLM was given, which sources it cited, the gate scores
  4. indexing.answer_score turns the records into numbers

The model is tested ALONE (a chain of one model, no fallback) so its quality is not mixed with another model's.
LLM replies are cached on disk (evals/.cache/llm): re-running costs nothing and gives the same answers.
Groq's rate limit (qwen: 7,000 input tokens a minute) makes a full run take about 10-15 minutes the first time.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from indexing import answer as answer_mod
from indexing import search_eval
from indexing.answer import Answer
from indexing.answer_eval import CachingLLM
from indexing.answer_golden import Golden, corpus_paths, load_golden
from indexing.answer_score import format_report, score_all, summarize
from indexing.llm import LLMAuthError
from ingestion.config import PROJECT_ROOT, settings

RUNS = PROJECT_ROOT / "evals" / "runs"
BASELINE = PROJECT_ROOT / "evals" / "answers_v2_baseline.json"


def to_record(question_id: str, a: Answer, llm_ms: int = 0) -> dict:
    """Everything needed to judge an answer later, without calling anything again."""
    return {
        "id": question_id, "q": a.question, "status": a.status, "text": a.text, "model": a.model, "fell_back": a.fell_back,
        "latency_ms": llm_ms or a.latency_ms, "prompt_version": a.prompt_version, "raw": a.raw,
        "context": [{"file": h.file_name, "heading": h.heading_path, "chunk_id": h.chunk_id, "content": h.content} for h in a.hits],
        "citations": [{"n": c.n, "chunk_id": c.chunk_id} for c in a.citations],
        "gate": {"answerable": a.decision.answerable, "top_rerank": a.decision.top_rerank, "top_vector": a.decision.top_vector},
    }


STALL_AFTER = 5                      # this many questions in a row without an LLM answer = the LLM is not answering: stop


class LLMStalled(RuntimeError):
    """The LLM failed for several questions in a row (most likely a rate limit or a daily quota)."""


def _answer_all(questions, llm, k, progress, stall_after: int | None = STALL_AFTER) -> list[dict]:
    records, start, stalled = [], time.time(), 0
    for i, q in enumerate(questions, 1):
        try:
            a = answer_mod.answer(q.q, k=k, llm=llm, principal=search_eval.EVAL_ASKER)
            records.append(to_record(q.id, a, llm_ms=getattr(llm, "last_ms", 0) if a.model else 0))
            if a.status == "llm_unavailable":
                stalled += 1
                if stall_after and stalled >= stall_after:
                    raise LLMStalled(f"the LLM did not answer {stalled} questions in a row (after {i} of {len(questions)}): probably a rate limit or a daily quota. "
                                     "Answers already received are cached: run the same command again later and it continues where it stopped.")
            elif a.model:
                stalled = 0
        except (LLMAuthError, LLMStalled):
            raise                                               # a bad key, or an LLM that is not answering, would fail every question: stop now
        except Exception as e:                                   # one broken question must not lose the whole run
            records.append({"id": q.id, "q": q.q, "status": "error", "text": "", "error": f"{type(e).__name__}: {e}",
                            "context": [], "citations": []})
        if progress and (i % 10 == 0 or i == len(questions)):
            progress(f"  {i}/{len(questions)} questions, {int(time.time() - start)} s")
    return records


def generate_repeats(golden: Golden, model: str | None = None, repeats: int = 1, llm=None, ids: list[str] | None = None,
                     progress=None, first_salt: int = 0, k: int | None = None, stall_after: int | None = STALL_AFTER) -> list[list[dict]]:
    """
    The same pipeline `repeats` times, in ONE throwaway database. Repeat j uses cache salt first_salt + j, so the LLM is really asked again
    (salt 0 is the run that may already be cached). `llm` can be a stand-in (tests); `model` forces a one-model chain;
    `k` = how many excerpts the LLM gets (default: the setting).
    """
    questions = [q for q in golden.questions if not ids or q.id in ids]
    paths = corpus_paths(golden.documents)
    saved = (settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model)
    if model:
        settings.llm_main_model = settings.llm_fallback_model = settings.llm_small_model = model
    llm = llm or CachingLLM(max_wait=70, retries=3)
    runs = []
    try:
        with search_eval.temp_database():
            search_eval.index_golden_documents(golden.documents, resolve=lambda n: paths[n])
            for j in range(repeats):
                llm.salt = first_salt + j
                if progress and repeats > 1:
                    progress(f"--- run {j + 1} of {repeats} (cache salt {first_salt + j})")
                runs.append(_answer_all(questions, llm, k, progress, stall_after))
    finally:
        settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model = saved
    return runs


def generate(golden: Golden, model: str | None = None, llm=None, ids: list[str] | None = None, progress=None,
             salt: int = 0, k: int | None = None) -> list[dict]:
    """One run of the pipeline on the questions (all, or just `ids`)."""
    return generate_repeats(golden, model, 1, llm=llm, ids=ids, progress=progress, first_salt=salt, k=k)[0]


def run_meta(golden: Golden, model: str | None, records: list[dict], salt: int = 0, k: int | None = None) -> dict:
    models = sorted({r.get("model") for r in records if r.get("model")})
    return {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model or settings.llm_main_model,
            "models_seen": models, "prompt_version": answer_mod.PROMPT_VERSION, "golden_version": golden.version,
            "questions": len(records), "search_mode": settings.search_mode, "rerank_candidates": settings.rerank_candidates,
            "rrf_keyword_weight": settings.rrf_keyword_weight, "min_rerank_score": settings.min_rerank_score,
            "min_vector_score": settings.min_vector_score, "context_chunks": k or settings.answer_context_chunks, "cache_salt": salt}


def save_run(meta: dict, records: list[dict], label: str | None = None) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", (label or meta["model"]).lower()).strip("_")
    path = RUNS / f"answers_{slug}_{meta['created'][:19].replace(':', '').replace('-', '')}.json"
    for target in (path, RUNS / "latest.json"):
        target.write_text(json.dumps({"meta": meta, "records": records}, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.answer_run")
    ap.add_argument("--model", help="Groq model id to test alone (default: the main model)")
    ap.add_argument("--ids", help="comma-separated question ids, e.g. A001,U005")
    ap.add_argument("--label", help="name used in the saved file")
    ap.add_argument("--rescore", help="score the records in this saved run again; calls no LLM")
    ap.add_argument("--save-baseline", action="store_true", help="keep this run as evals/answers_v2_baseline.json")
    ap.add_argument("--repeat", type=int, default=1, help="run the pipeline this many times (each time the LLM is really asked again) and report the spread")
    ap.add_argument("--chunks", type=int, default=None, help="how many excerpts the LLM gets (default: the setting; fewer = a deliberately weaker system)")
    args = ap.parse_args(argv)

    golden = load_golden()
    if args.rescore:
        data = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        meta, records = data["meta"], data["records"]
        print(f"re-scoring {len(records)} saved answers from {args.rescore} (model {meta.get('model')}); no LLM calls")
    else:
        from indexing.db import can_connect
        if not can_connect():
            print("Database not reachable: start it with  docker compose up -d")
            return 1
        model = args.model or settings.llm_main_model
        ids = [i.strip() for i in args.ids.split(",")] if args.ids else None
        print(f"running the real pipeline with {model} alone (no fallback), {args.repeat} time(s)"
              f"{f', {args.chunks} excerpts' if args.chunks else ''}; replies are cached, so a re-run is free")
        try:
            runs = generate_repeats(golden, model, args.repeat, ids=ids, progress=print, k=args.chunks)
        except LLMAuthError as e:
            print(f"LLM key problem: {e}")
            return 1
        except LLMStalled as e:
            print(f"STOPPED: {e}")
            return 2
        base_label = args.label or (f"{model}_k{args.chunks}" if args.chunks else model)
        saved_paths = []
        for j, records in enumerate(runs):
            meta = run_meta(golden, model, records, salt=j, k=args.chunks)
            saved_paths.append(save_run(meta, records, f"{base_label}_r{j + 1}" if args.repeat > 1 else base_label))
        print("saved:\n  " + "\n  ".join(str(p) for p in saved_paths))
        if args.repeat > 1:
            from indexing.answer_stats import print_stability, stability_report
            scored_runs = [score_all(golden, r) for r in runs]
            incomplete = False
            for j, sc in enumerate(scored_runs, 1):
                s = summarize(sc)
                incomplete = incomplete or bool(s["unavailable"])
                print(f"run {j}: correct {s['answerable']['accuracy']['k']}/{s['n_answerable']}, unanswerable refused {s['unanswerable']['refused_ok']['k']}/{s['n_unanswerable']}"
                      f"{', NEVER ANSWERED BY THE LLM: ' + str(s['unavailable']) if s['unavailable'] else ''}")
            print_stability(stability_report(golden, scored_runs, runs, [model] * len(runs)), golden)
            return 2 if incomplete else 0
        records = runs[0]

    meta["scored_with_golden_version"] = golden.version             # the same saved answers can be scored with a newer key
    scored = score_all(golden, records)
    summary = summarize(scored)
    print(format_report(summary, scored, golden))
    if args.save_baseline and summary["unavailable"]:
        print("\nNOT saving a baseline: this run is incomplete (the LLM never answered some questions).")
        return 2
    if args.save_baseline:
        BASELINE.write_text(json.dumps({"meta": meta, "summary": summary, "records": records}, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\nBaseline saved: {BASELINE.relative_to(PROJECT_ROOT)}")
    return 2 if summary["unavailable"] else 0


if __name__ == "__main__":
    sys.exit(main())
