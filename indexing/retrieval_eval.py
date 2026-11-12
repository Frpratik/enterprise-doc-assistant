"""
Retrieval evaluation: does search find the chunk that holds the answer?

For every golden question we rank ALL chunks by similarity and look at the top results:
  recall@k   share of questions whose answer is in the top k chunks   (k = 1, 3, 5)
  MRR        mean reciprocal rank: answer at #1 -> 1.0, at #2 -> 0.5, at #3 -> 0.33, missing -> 0

A chunk is relevant when it comes from an expected file AND contains the answer phrase,
so different chunking methods (different chunk boundaries) are compared fairly.

Chunking methods compared:
  structure      ours: sections / slides / tables + heading path in the embedded text
  structure_raw  ours, but embedding only the content (no title / heading path)   -> what is the context worth?
  recursive      LangChain-style RecursiveCharacterTextSplitter: 500 chars, 100 overlap
  fixed          naive: cut every 500 characters, no overlap

  python -m indexing.retrieval_eval                      all methods, compare with baseline
  python -m indexing.retrieval_eval --save-baseline
  python -m indexing.retrieval_eval --methods structure  just one
Parsed documents and embeddings are cached in evals/.cache, so re-runs take seconds.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np

from indexing.chunker import chunk_document
from indexing.embeddings import get_embedder
from ingestion.clean import clean_document
from ingestion.config import PROJECT_ROOT, settings
from ingestion.models import Document
from ingestion.registry import get_parser
from ingestion.safety import FILE_TYPES, sha256

GOLDEN = PROJECT_ROOT / "evals" / "retrieval_golden.json"
SAMPLES = PROJECT_ROOT / "data" / "samples" / "good"
CACHE = PROJECT_ROOT / "evals" / ".cache"
BASELINE = PROJECT_ROOT / "evals" / "retrieval_baseline.json"
LATEST = PROJECT_ROOT / "evals" / "retrieval_latest.json"
METHODS = ["structure", "structure_raw", "recursive", "fixed"]
KS = (1, 3, 5)


# ---------------- documents (parsed once, cached) ----------------
def load_document(path: Path) -> Document:
    key = f"{sha256(path)[:16]}_{settings.parser_version}"
    cached = CACHE / "docs" / f"{path.name}.{key}.json"
    if cached.exists():
        return Document.model_validate_json(cached.read_text(encoding="utf-8"))
    ft = FILE_TYPES[path.suffix.lower()]
    doc = Document(doc_id=path.stem, file_name=path.name, file_type=ft)
    for el in get_parser(ft).parse(doc, path):
        doc.add(el)
    clean_document(doc)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(doc.model_dump_json(), encoding="utf-8")
    return doc


# ---------------- chunking methods ----------------
def chunks_structure(doc: Document, with_context: bool = True) -> list[tuple[str, str]]:
    """(content, text_to_embed)"""
    return [(c.content, c.embed_text if with_context else c.content) for c in chunk_document(doc)]


def chunks_fixed(doc: Document, size: int = 500) -> list[tuple[str, str]]:
    text = doc.full_text()
    return [(text[i:i + size], text[i:i + size]) for i in range(0, len(text), size) if text[i:i + size].strip()]


def chunks_recursive(doc: Document, size: int = 500, overlap: int = 100) -> list[tuple[str, str]]:
    """Like LangChain's RecursiveCharacterTextSplitter: try to cut at paragraphs, then lines,
    then spaces, so pieces stay under `size` characters; neighbouring pieces overlap."""
    def split(text: str, seps: list[str]) -> list[str]:
        if len(text) <= size or not seps:
            return [text]
        sep, rest = seps[0], seps[1:]
        parts = text.split(sep) if sep else list(text)
        out, cur = [], ""
        for p in parts:
            candidate = (cur + sep + p) if cur else p
            if len(candidate) <= size:
                cur = candidate
            else:
                if cur:
                    out.append(cur)
                cur = p if len(p) <= size else ""
                if len(p) > size:
                    out += split(p, rest)
        if cur:
            out.append(cur)
        return out

    pieces = [p for p in split(doc.full_text(), ["\n\n", "\n", " ", ""]) if p.strip()]
    merged = []
    for i, p in enumerate(pieces):                       # add overlap from the previous piece
        prev_tail = pieces[i - 1][-overlap:] if i and overlap else ""
        text = (prev_tail + " " + p).strip() if prev_tail else p
        merged.append((text, text))
    return merged


def make_chunks(method: str, doc: Document):
    return {"structure": lambda: chunks_structure(doc, True),
            "structure_raw": lambda: chunks_structure(doc, False),
            "recursive": lambda: chunks_recursive(doc),
            "fixed": lambda: chunks_fixed(doc)}[method]()


# ---------------- embeddings (cached on disk) ----------------
class EmbeddingCache:
    def __init__(self, model):
        self.model = model
        slug = re.sub(r"[^a-z0-9]+", "_", model.name.lower())
        self.path = CACHE / f"emb_{slug}.npz"
        self.data = dict(np.load(self.path)) if self.path.exists() else {}
        self.new = 0

    def docs(self, texts: list[str]) -> np.ndarray:
        keys = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        missing = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in self.data]
        if missing:
            for (k, _), v in zip(missing, self.model.embed_documents([t for _, t in missing])):
                self.data[k] = v
            self.new += len(missing)
        return np.array([self.data[k] for k in keys])

    def query(self, q: str) -> np.ndarray:
        k = "q:" + hashlib.sha256(q.encode()).hexdigest()
        if k not in self.data:
            self.data[k] = self.model.embed_query(q)
            self.new += 1
        return self.data[k]

    def save(self):
        if self.new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            np.savez(self.path, **self.data)


# ---------------- scoring ----------------
def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def is_relevant(file_name: str, content: str, answers: list[list[str]]) -> bool:
    c = norm(content)
    return any(file_name == f and norm(phrase) in c for f, phrase in answers)


def evaluate_method(method: str, docs: list[Document], questions: list[dict], cache: EmbeddingCache) -> dict:
    chunks = [(d.file_name, content, emb) for d in docs for content, emb in make_chunks(method, d)]
    vectors = cache.docs([emb for _, _, emb in chunks])
    per_q, ranks = [], []
    for item in questions:
        scores = vectors @ cache.query(item["q"])
        order = np.argsort(-scores)
        rank = next((i + 1 for i, idx in enumerate(order)
                     if is_relevant(chunks[idx][0], chunks[idx][1], item["answers"])), None)
        ranks.append(rank)
        top = chunks[order[0]]
        per_q.append({"q": item["q"], "rank": rank, "top_file": top[0], "top_text": top[1][:90]})
    n = len(questions)
    metrics = {f"recall@{k}": round(sum(1 for r in ranks if r and r <= k) / n, 3) for k in KS}
    metrics["mrr"] = round(sum(1 / r for r in ranks if r) / n, 3)
    return {"chunks": len(chunks), **metrics, "questions": per_q}


def run(methods: list[str]) -> dict:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    docs = [load_document(SAMPLES / name) for name in golden["documents"]]
    cache = EmbeddingCache(get_embedder())
    try:
        results = {m: evaluate_method(m, docs, golden["questions"], cache) for m in methods}
    finally:
        cache.save()
    return {"golden_version": golden["version"], "embed_model": cache.model.name,
            "chunk_target_tokens": settings.chunk_target_tokens, "questions": len(golden["questions"]),
            "methods": results}


def print_report(result: dict, baseline: dict | None) -> None:
    base = (baseline or {}).get("methods", {})
    print(f"\n{result['questions']} questions, model {result['embed_model']}\n")
    print(f"{'METHOD':16}{'CHUNKS':>7}{'R@1':>8}{'R@3':>8}{'R@5':>8}{'MRR':>8}")
    for m, r in result["methods"].items():
        line = f"{m:16}{r['chunks']:>7}" + "".join(f"{r[k]:>8.3f}" for k in ("recall@1", "recall@3", "recall@5", "mrr"))
        if m in base:
            d = r["mrr"] - base[m]["mrr"]
            line += "   (MRR same as baseline)" if abs(d) < 0.0005 else f"   (MRR {'+' if d > 0 else ''}{d:.3f} vs baseline)"
        print(line)
    for m, r in result["methods"].items():
        misses = [q for q in r["questions"] if not q["rank"] or q["rank"] > 1]
        if m == "structure" and misses:
            print(f"\nNot ranked #1 with '{m}':")
            for q in misses:
                print(f"  rank {q['rank'] or 'not found':>9}  {q['q']}\n                  top was: {q['top_file']} | {q['top_text']!r}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.retrieval_eval")
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--save-baseline", action="store_true")
    args = ap.parse_args(argv)
    result = run([m.strip() for m in args.methods.split(",")])
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
    print_report(result, baseline)
    LATEST.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.save_baseline:
        BASELINE.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nBaseline saved: {BASELINE.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
