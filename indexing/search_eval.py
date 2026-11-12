"""
Search evaluation: does the REAL search (database + vector + keyword + RRF) find the answer chunk?

Same 30 golden questions as retrieval_eval, but this time the documents are indexed into a
throwaway database and searched with the real code, in 3 modes:
  vector   meaning only          keyword   words only          hybrid   both, merged with RRF

  python -m indexing.search_eval                 compare the 3 modes with the saved baseline
  python -m indexing.search_eval --save-baseline

Your real data is never touched: everything runs in a temporary database that is dropped afterwards.
"""
import argparse
import json
import sys
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import make_conninfo

from indexing import db
from indexing.doc_store import PgManifest
from indexing.embeddings import get_embedder
from indexing.indexer import index_document
from indexing.migrate import migrate
from indexing.retrieval_eval import GOLDEN, SAMPLES, is_relevant, load_document
from indexing.search import search
from ingestion.config import PROJECT_ROOT, settings
from ingestion.models import Status
from ingestion.safety import sha256

BASELINE = PROJECT_ROOT / "evals" / "search_baseline.json"
LATEST = PROJECT_ROOT / "evals" / "search_latest.json"
MODES = ["vector", "keyword", "hybrid"]
KS = (1, 3, 5)


@contextmanager
def temp_database():
    """A brand-new empty database with all migrations, dropped afterwards. settings.database_url points at it meanwhile."""
    real = settings.database_url
    name = f"eda_eval_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(real, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(real, dbname=name)
    settings.database_url = url
    try:
        migrate(url, quiet=True)
        yield url
    finally:
        db.close_all()
        settings.database_url = real
        with psycopg.connect(real, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def index_golden_documents(names: list[str]) -> int:
    total = 0
    for name in names:
        path = SAMPLES / name
        doc = load_document(path)
        doc.hash = sha256(path)
        doc.mark(Status.DONE, "eval")
        PgManifest().save(doc)
        result = index_document(doc)
        if not result.indexed:
            raise RuntimeError(f"{name} was not indexed: {result.reason}")
        total += result.chunks
    return total


def evaluate_mode(mode: str, questions: list[dict], depth: int = 60) -> dict:
    ranks, per_q = [], []
    for item in questions:
        hits = search(item["q"], k=depth, mode=mode, candidates=depth)
        rank = next((i for i, h in enumerate(hits, 1) if is_relevant(h.file_name, h.content, item["answers"])), None)
        ranks.append(rank)
        per_q.append({"q": item["q"], "rank": rank,
                      "top": f"{hits[0].file_name} > {hits[0].heading_path.split(' > ')[-1]}" if hits else "(nothing found)"})
    n = len(questions)
    metrics = {f"recall@{k}": round(sum(1 for r in ranks if r and r <= k) / n, 3) for k in KS}
    metrics["mrr"] = round(sum(1 / r for r in ranks if r) / n, 3)
    return {**metrics, "questions": per_q}


SETS = {"paraphrase": GOLDEN, "exact_terms": PROJECT_ROOT / "evals" / "search_exact_terms.json"}


def run(modes: list[str] | None = None) -> dict:
    sets = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in SETS.items()}
    docs = list(dict.fromkeys(d for g in sets.values() for d in g["documents"]))
    with temp_database():
        chunks = index_golden_documents(docs)
        results = {name: {m: evaluate_mode(m, g["questions"]) for m in (modes or MODES)} for name, g in sets.items()}
    return {"chunks": chunks, "embed_model": get_embedder().name, "rrf_keyword_weight": settings.rrf_keyword_weight,
            "questions": {name: len(g["questions"]) for name, g in sets.items()}, "sets": results}


def print_report(result: dict, baseline: dict | None) -> None:
    print(f"\n{result['chunks']} chunks, real database search, keyword weight {result['rrf_keyword_weight']}")
    for name, modes in result["sets"].items():
        base = ((baseline or {}).get("sets", {}).get(name) or {})
        print(f"\n--- {name}: {result['questions'][name]} questions ---")
        print(f"{'MODE':10}{'R@1':>8}{'R@3':>8}{'R@5':>8}{'MRR':>8}")
        for m, r in modes.items():
            line = f"{m:10}" + "".join(f"{r[k]:>8.3f}" for k in ("recall@1", "recall@3", "recall@5", "mrr"))
            if m in base:
                d = r["mrr"] - base[m]["mrr"]
                line += "   (same as baseline)" if abs(d) < 0.0005 else f"   (MRR {'+' if d > 0 else ''}{d:.3f} vs baseline)"
            print(line)
        hybrid, vector = modes.get("hybrid"), modes.get("vector")
        if hybrid and vector:
            diff = [(h, v) for h, v in zip(hybrid["questions"], vector["questions"]) if h["rank"] != v["rank"]]
            for h, v in diff:
                print(f"  vector {str(v['rank']):>4} -> hybrid {str(h['rank']):>4}   {h['q']}")
            for q in (q for q in hybrid["questions"] if not q["rank"] or q["rank"] > 1):
                print(f"  hybrid rank {q['rank'] or 'not found':>9}: {q['q']}   (top was: {q['top']})")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.search_eval")
    ap.add_argument("--save-baseline", action="store_true")
    args = ap.parse_args(argv)
    result = run()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    print_report(result, baseline)
    LATEST.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.save_baseline:
        BASELINE.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nBaseline saved: {BASELINE.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
