"""
Search = find the active chunks that best match a question, using TWO searches and merging them.

  vector search   finds chunks with the same MEANING   ("internet claim" -> "Reimbursement Limits")
  keyword search  finds chunks with the same WORDS     (exact terms: "HRA", "ext 4455", "26 weeks")
  RRF             merges the two ranked lists: a chunk near the top of BOTH wins

  python -m indexing.search "how many sick leaves do I get"
  python -m indexing.search "notice period" -k 3 --full
  python -m indexing.search "HRA percentage" --mode vector      (or keyword / hybrid)

Only ACTIVE chunks are returned (latest approved version of documents that weren't deleted).
Every hit keeps its raw vector similarity (`vector_score`), because the RRF score only says how the
chunk ranked, not whether it is actually relevant. The "I don't know" rule needs the real similarity.
"""
import argparse
import re
import sys
from dataclasses import dataclass

from indexing.db import connect
from indexing.embeddings import Embedder, get_embedder
from ingestion.config import settings


@dataclass
class Hit:
    score: float                        # the final ranking score (RRF in hybrid mode, cosine in vector mode)
    chunk_id: str
    doc_id: str
    version: int
    file_name: str
    heading_path: str
    location: dict
    chunk_type: str
    content: str
    vector_score: float = 0.0           # cosine similarity between the question and this chunk (0 - 1)
    keyword_score: float | None = None  # keyword rank score, None if the keyword search did not find it
    found_by: str = "vector"            # "vector", "keyword" or "both"

    def where(self) -> str:
        loc = self.location or {}
        if "pages" in loc:
            return "page " + ", ".join(map(str, loc["pages"]))
        if "slides" in loc:
            return "slide " + ", ".join(map(str, loc["slides"]))
        if "sheet" in loc:
            return f"sheet '{loc['sheet']}'"
        return ""


SELECT_PART = "c.chunk_id, c.doc_id, c.version, d.file_name, c.heading_path, c.location, c.chunk_type, c.content"


def keyword_terms(question: str) -> list[str]:
    """
    Split a question into search terms. Words keep their inner punctuation, so an e-mail address
    ('it@acme-example.com') or an amount ('25,000') stays ONE term and matches exactly.
    Postgres later drops stop words (how, many, it) and stems the rest ('leaves' matches 'leave').
    """
    terms = (w.strip(".,?!;:\"'()[]|") for w in question.lower().split())
    return list(dict.fromkeys(t for t in terms if t))


def _vector_candidates(conn, qvec, embedder: Embedder, n: int) -> list[tuple]:
    dims = embedder.dims
    conn.execute(f"SET LOCAL hnsw.ef_search = {int(settings.search_ef)}")
    return conn.execute(
        f"""SELECT 1 - ((e.embedding::vector({dims})) <=> %(q)s::vector({dims})) AS vscore, {SELECT_PART}
            FROM embeddings e
            JOIN chunks c    ON c.embed_hash = e.embed_hash
            JOIN documents d ON d.doc_id = c.doc_id AND d.version = c.version
            WHERE e.embed_model = %(model)s AND c.is_active AND d.deleted_at IS NULL
            ORDER BY (e.embedding::vector({dims})) <=> %(q)s::vector({dims})
            LIMIT %(n)s""",
        {"q": qvec, "model": embedder.name, "n": n}).fetchall()


def _keyword_candidates(conn, question: str, qvec, embedder: Embedder, n: int) -> list[tuple]:
    """
    Keyword search with IDF weighting (the idea behind BM25), done in Postgres:
      every term becomes its own tsquery; a term's weight is how RARE it is among the active chunks
      (a term found in 1 chunk of 33 counts far more than one found in 20 of 33);
      a chunk's score = the sum of the weights of the terms it contains.
    Plain ts_rank cannot do this: it would rate 'employees' and '4455' the same.
    """
    terms = keyword_terms(question)
    if not terms:
        return []
    dims = embedder.dims
    # the vector similarity is computed too, so a keyword-only hit still carries a real relevance number
    return conn.execute(
        f"""WITH active AS (
                SELECT c.chunk_id, c.tsv FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id AND d.version = c.version
                WHERE c.is_active AND d.deleted_at IS NULL),
            total AS (SELECT count(*)::float AS n FROM active),
            term_q AS (SELECT plainto_tsquery('english', t) AS query FROM unnest(%(terms)s::text[]) AS t),
            idf AS (
                SELECT term_q.query, ln(1 + (total.n - cnt.c + 0.5) / (cnt.c + 0.5)) AS w
                FROM term_q, total,
                     LATERAL (SELECT count(*)::float AS c FROM active a WHERE a.tsv @@ term_q.query) AS cnt
                WHERE cnt.c > 0)
            SELECT sum(idf.w) AS kscore,
                   1 - ((e.embedding::vector({dims})) <=> %(q)s::vector({dims})) AS vscore, {SELECT_PART}
            FROM idf
            JOIN chunks c     ON c.tsv @@ idf.query
            JOIN documents d  ON d.doc_id = c.doc_id AND d.version = c.version
            JOIN embeddings e ON e.embed_hash = c.embed_hash AND e.embed_model = %(model)s
            WHERE c.is_active AND d.deleted_at IS NULL
            GROUP BY c.chunk_id, d.doc_id, d.version, e.embed_hash, e.embed_model
            ORDER BY kscore DESC, c.chunk_id
            LIMIT %(n)s""",
        {"q": qvec, "model": embedder.name, "terms": terms, "n": n}).fetchall()


def rrf_merge(vector_ids: list[str], keyword_ids: list[str], k: int | None = None,
              keyword_weight: float | None = None) -> dict[str, float]:
    """
    Reciprocal Rank Fusion: every list gives a chunk 1 / (k + rank). A chunk found by both lists
    gets both. Only RANKS are used, never the raw scores, because cosine similarity (0-1) and
    keyword scores live on different scales and cannot be added fairly.
    """
    k = k or settings.rrf_k
    kw = settings.rrf_keyword_weight if keyword_weight is None else keyword_weight
    scores: dict[str, float] = {}
    for ids, weight in ((vector_ids, 1.0), (keyword_ids, kw)):
        for rank, chunk_id in enumerate(ids, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)
    return scores


def search(query: str, k: int | None = None, embedder: Embedder | None = None, mode: str | None = None,
           candidates: int | None = None) -> list[Hit]:
    embedder = embedder or get_embedder()
    k = k or settings.search_top_k
    mode = mode or settings.search_mode
    n = max(candidates or settings.search_candidates, k)
    if mode not in ("hybrid", "vector", "keyword"):
        raise ValueError(f"unknown search mode '{mode}' (use hybrid, vector or keyword)")
    qvec = embedder.embed_query(query)

    with connect() as conn:
        vec_rows = _vector_candidates(conn, qvec, embedder, n) if mode in ("hybrid", "vector") else []
        kw_rows = _keyword_candidates(conn, query, qvec, embedder, n) if mode in ("hybrid", "keyword") else []

    hits: dict[str, Hit] = {}
    for r in vec_rows:                                    # r = (vscore, chunk_id, ...)
        v = round(float(r[0]), 3)
        hits[r[1]] = Hit(v, *r[1:], vector_score=v, found_by="vector")
    for r in kw_rows:                                     # r = (kscore, vscore, chunk_id, ...)
        h = hits.get(r[2])
        if h:
            h.keyword_score, h.found_by = round(float(r[0]), 3), "both"
        else:
            hits[r[2]] = Hit(0.0, *r[2:], vector_score=round(float(r[1]), 3),
                             keyword_score=round(float(r[0]), 3), found_by="keyword")

    if mode == "hybrid":
        fused = rrf_merge([r[1] for r in vec_rows], [r[2] for r in kw_rows])
        for cid, h in hits.items():
            h.score = round(fused[cid], 4)
        ordered = sorted(hits.values(), key=lambda h: (-h.score, -h.vector_score))
    elif mode == "keyword":
        for h in hits.values():
            h.score = h.keyword_score or 0.0
        ordered = sorted(hits.values(), key=lambda h: -h.score)
    else:
        ordered = sorted(hits.values(), key=lambda h: -h.score)
    return ordered[:k]


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.search")
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=settings.search_top_k, help="how many results")
    ap.add_argument("--full", action="store_true", help="show the whole chunk text")
    ap.add_argument("--mode", choices=["hybrid", "vector", "keyword"], default=settings.search_mode)
    args = ap.parse_args(argv)

    from indexing.db import can_connect
    if not can_connect():
        print("Database not reachable: start it with  docker compose up -d")
        return 1
    hits = search(args.query, args.k, mode=args.mode)
    if not hits:
        print("No results (is anything indexed? try: python -m ingestion.run --file <file>)")
        return 0
    print(f'\nQ: "{args.query}"   [{args.mode} search]\n')
    for i, h in enumerate(hits, 1):
        print(f"{i}. [{h.score:.3f} | meaning {h.vector_score:.2f} | {h.found_by}] {h.file_name} > "
              f"{h.heading_path.split(' > ', 1)[-1]}{'  (' + h.where() + ')' if h.where() else ''}")
        text = h.content if args.full else (h.content[:220] + (" ..." if len(h.content) > 220 else ""))
        print("   " + text.replace("\n", "\n   ") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
