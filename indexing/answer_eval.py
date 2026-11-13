"""
Answer evaluation: does the whole pipeline answer what it should, cite the right source, and REFUSE what it should?

Every question goes through the real pipeline (search -> rerank -> gate -> LLM -> citation checks) once per model,
with NO fallback (a one-model chain), so the models can be compared fairly.

Groups:
  answerable  (44)  the answer IS in the documents
  near_topic  (22)  sounds like our documents, but the answer is NOT there   -> must be refused
  off_topic   (10)  unrelated to the company                                  -> must be refused (the gate does it)

What is measured (and what is NOT):
  answered            share of answerable questions that got an answer
  cited right chunk   the chunk behind a citation is the right one (right file AND contains the answer phrase)
  numbers match       every number in the expected answer phrase appears in the answer text.
                      A rough proxy only: it cannot tell if a sentence is well written or if it adds a wrong claim.
                      Real answer-quality scoring (faithfulness, relevancy) is Phase 4.
  refused             near-topic / off-topic questions that were NOT answered

LLM replies are cached on disk (evals/.cache/llm), so re-runs are free and repeatable (temperature 0).

  python -m indexing.answer_eval                         all three models
  python -m indexing.answer_eval --models openai/gpt-oss-120b
  python -m indexing.answer_eval --save-baseline
"""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from indexing import answer as answer_mod
from indexing import search_eval
from indexing.abstain_eval import load_questions
from indexing.llm import LLMClient
from indexing.retrieval_eval import is_relevant
from ingestion.config import PROJECT_ROOT, settings

BASELINE = PROJECT_ROOT / "evals" / "answer_baseline.json"
LATEST = PROJECT_ROOT / "evals" / "answer_latest.json"
CACHE = PROJECT_ROOT / "evals" / ".cache" / "llm"


class CachingLLM(LLMClient):
    """Same client, but every reply is stored on disk by (model, prompt): re-runs cost nothing and never change.
    The ORIGINAL call time is stored too (last_ms), so reports show real latency, not the speed of the cache."""

    last_ms = 0

    def _call(self, model, messages, max_tokens, temperature):
        key = hashlib.sha256(json.dumps([model, messages, temperature], sort_keys=True).encode()).hexdigest()
        path = CACHE / f"{key}.json"
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            self.last_ms = d.get("ms", 0)
            return d["text"], d["usage"]
        t0 = time.perf_counter()
        text, usage = super()._call(model, messages, max_tokens, temperature)
        self.last_ms = int((time.perf_counter() - t0) * 1000)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": model, "text": text, "usage": usage, "ms": self.last_ms}), encoding="utf-8")
        return text, usage


def numbers_match(expected_phrases: list[str], text: str) -> bool | None:
    """None when no expected phrase contains a number (the check does not apply)."""
    def nums(s):
        return set(re.findall(r"\d[\d,.]*\d|\d", s.replace("‑", "-")))
    clean = lambda s: {n.replace(",", "").rstrip(".") for n in nums(s)}
    have = clean(text)
    checks = [clean(p) for p in expected_phrases if clean(p)]
    if not checks:
        return None
    return any(c <= have for c in checks)


def load_answerable() -> list[tuple[str, list]]:
    out = []
    for path in search_eval.SETS.values():
        out += [(q["q"], q["answers"]) for q in json.loads(path.read_text(encoding="utf-8"))["questions"]]
    return out


def run(models: list[str] | None = None) -> dict:
    models = models or [settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model]
    answerable = load_answerable()
    qs = load_questions()
    docs = list(dict.fromkeys(d for p in search_eval.SETS.values()
                              for d in json.loads(p.read_text(encoding="utf-8"))["documents"]))
    saved = (settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model)
    real_retrieve, memo = answer_mod.retrieve, {}

    def cached_retrieve(q, k=None, embedder=None):               # the search part is identical for every model
        if q not in memo:
            memo[q] = real_retrieve(q, k=k, embedder=embedder)
        return memo[q]

    results = {}
    try:
        with search_eval.temp_database():
            search_eval.index_golden_documents(docs)
            answer_mod.retrieve = cached_retrieve
            for model in models:
                # a chain of ONE model: no fallback, so we measure this model alone
                settings.llm_main_model = settings.llm_fallback_model = settings.llm_small_model = model
                llm = CachingLLM(max_wait=70, retries=3)
                rows = {"answerable": [], "near_topic": [], "off_topic": []}
                for q, answers in answerable:
                    a = answer_mod.answer(q, llm=llm)
                    cited = [h for c in a.citations for h in a.hits if h.chunk_id == c.chunk_id]
                    rows["answerable"].append({
                        "q": q, "status": a.status, "text": a.text, "model": a.model, "ms": llm.last_ms if a.model else 0,
                        "cited_right": any(is_relevant(h.file_name, h.content, answers) for h in cited) if cited else None,
                        "numbers_match": numbers_match([p for _, p in answers], a.text) if a.status == "answered" else None,
                        "invalid_citations": a.invalid_citations})
                for group in ("near_topic", "off_topic"):
                    for q in qs[group]:
                        a = answer_mod.answer(q, llm=llm)
                        rows[group].append({"q": q, "status": a.status, "text": a.text, "ms": a.latency_ms})
                results[model] = rows
    finally:
        answer_mod.retrieve = real_retrieve
        settings.llm_main_model, settings.llm_fallback_model, settings.llm_small_model = saved
    return {"prompt_version": answer_mod.PROMPT_VERSION, "models": {m: {"summary": summarize(r), "rows": r}
                                                                      for m, r in results.items()}}


def summarize(rows: dict) -> dict:
    a, near, off = rows["answerable"], rows["near_topic"], rows["off_topic"]
    answered = [r for r in a if r["status"] == "answered"]
    num = [r["numbers_match"] for r in answered if r["numbers_match"] is not None]
    ms = [r["ms"] for r in answered if r["ms"]]
    return {
        "answerable_answered": round(len(answered) / len(a), 3),
        "answerable_refused_by_llm": sum(r["status"] == "refused_llm" for r in a),
        "answerable_uncited": sum(r["status"] == "uncited" for r in a),
        "cited_right_chunk": round(sum(bool(r["cited_right"]) for r in answered) / max(len(answered), 1), 3),
        "numbers_match": round(sum(num) / max(len(num), 1), 3),
        "invalid_citations": sum(len(r["invalid_citations"]) for r in a),
        "near_topic_refused": round(sum(r["status"] != "answered" for r in near) / len(near), 3),
        "off_topic_refused": round(sum(r["status"] != "answered" for r in off) / len(off), 3),
        "median_llm_ms": sorted(ms)[len(ms) // 2] if ms else None,
    }


def print_report(res: dict, baseline: dict | None) -> None:
    base = (baseline or {}).get("models", {})
    print(f"\nprompt {res['prompt_version']}, no fallback (one model at a time)\n")
    cols = [("answered", "answerable_answered", "{:.0%}"), ("right chunk", "cited_right_chunk", "{:.0%}"),
            ("numbers ok", "numbers_match", "{:.0%}"), ("near refused", "near_topic_refused", "{:.0%}"),
            ("off refused", "off_topic_refused", "{:.0%}"), ("median ms", "median_llm_ms", "{}")]
    print(f"{'MODEL':24}" + "".join(f"{c[0]:>13}" for c in cols))
    for m, d in res["models"].items():
        s = d["summary"]
        print(f"{m:24}" + "".join(f"{c[2].format(s[c[1]]):>13}" for c in cols)
              + ("" if m not in base else "   (baseline: near " + f"{base[m]['summary']['near_topic_refused']:.0%})"))
    for m, d in res["models"].items():
        rows = d["rows"]
        print(f"\n=== {m} ===")
        for r in rows["answerable"]:
            if r["status"] != "answered":
                print(f"  ANSWERABLE NOT ANSWERED ({r['status']}): {r['q']}")
            elif r["cited_right"] is False:
                print(f"  wrong chunk cited: {r['q']}  ->  {r['text'][:90]}")
            elif r["numbers_match"] is False:
                print(f"  numbers differ: {r['q']}  ->  {r['text'][:100]}")
        for r in rows["near_topic"] + rows["off_topic"]:
            if r["status"] == "answered":
                print(f"  SHOULD HAVE REFUSED: {r['q']}  ->  {r['text'][:110]}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.answer_eval")
    ap.add_argument("--models", help="comma-separated Groq model ids (default: main, fallback, small)")
    ap.add_argument("--save-baseline", action="store_true")
    args = ap.parse_args(argv)
    res = run([m.strip() for m in args.models.split(",")] if args.models else None)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    print_report(res, baseline)
    LATEST.write_text(json.dumps(res, indent=2), encoding="utf-8")
    if args.save_baseline:
        BASELINE.write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"\nBaseline saved: {BASELINE.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
