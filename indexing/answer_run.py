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


def generate(golden: Golden, model: str | None = None, llm=None, ids: list[str] | None = None, progress=None) -> list[dict]:
    """Run the pipeline on the questions (all, or just `ids`). `llm` can be a stand-in (tests); `model` forces a one-model chain."""
    questions = [q for q in golden.questions if not ids or q.id in ids]
    paths = corpus_paths(golden.documents)
    saved = (settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model)
    if model:
        settings.llm_main_model = settings.llm_fallback_model = settings.llm_small_model = model
    llm = llm or CachingLLM(max_wait=70, retries=3)
    records, start = [], time.time()
    try:
        with search_eval.temp_database():
            search_eval.index_golden_documents(golden.documents, resolve=lambda n: paths[n])
            for i, q in enumerate(questions, 1):
                try:
                    a = answer_mod.answer(q.q, llm=llm)
                    records.append(to_record(q.id, a, llm_ms=getattr(llm, "last_ms", 0) if a.model else 0))
                except LLMAuthError:
                    raise                                           # a bad key would fail every question: stop now
                except Exception as e:                               # one broken question must not lose the whole run
                    records.append({"id": q.id, "q": q.q, "status": "error", "text": "", "error": f"{type(e).__name__}: {e}",
                                    "context": [], "citations": []})
                if progress and (i % 10 == 0 or i == len(questions)):
                    progress(f"  {i}/{len(questions)} questions, {int(time.time() - start)} s")
    finally:
        settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model = saved
    return records


def run_meta(golden: Golden, model: str | None, records: list[dict]) -> dict:
    models = sorted({r.get("model") for r in records if r.get("model")})
    return {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": model or settings.llm_main_model,
            "models_seen": models, "prompt_version": answer_mod.PROMPT_VERSION, "golden_version": golden.version,
            "questions": len(records), "search_mode": settings.search_mode, "rerank_candidates": settings.rerank_candidates,
            "rrf_keyword_weight": settings.rrf_keyword_weight, "min_rerank_score": settings.min_rerank_score,
            "min_vector_score": settings.min_vector_score, "context_chunks": settings.answer_context_chunks}


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
        print(f"running the real pipeline with {model} alone (no fallback); replies are cached, so a re-run is free")
        try:
            records = generate(golden, model, ids=ids, progress=print)
        except LLMAuthError as e:
            print(f"LLM key problem: {e}")
            return 1
        meta = run_meta(golden, model, records)
        print(f"saved: {save_run(meta, records, args.label)}")

    meta["scored_with_golden_version"] = golden.version             # the same saved answers can be scored with a newer key
    scored = score_all(golden, records)
    summary = summarize(scored)
    print(format_report(summary, scored, golden))
    if args.save_baseline:
        BASELINE.write_text(json.dumps({"meta": meta, "summary": summary, "records": records}, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\nBaseline saved: {BASELINE.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
