"""
Search = embed the question, then find the active chunks whose vectors are closest.

  python -m indexing.search "how many sick leaves do I get"
  python -m indexing.search "notice period" -k 3 --full

Only ACTIVE chunks are returned (latest approved version of documents that weren't deleted).
This is plain vector search; Phase 3 adds keyword search + reranking + the LLM answer.
"""
import argparse
import sys
from dataclasses import dataclass

from indexing.db import connect
from indexing.embeddings import Embedder, get_embedder
from ingestion.config import settings


@dataclass
class Hit:
    score: float
    chunk_id: str
    doc_id: str
    version: int
    file_name: str
    heading_path: str
    location: dict
    chunk_type: str
    content: str

    def where(self) -> str:
        loc = self.location or {}
        if "pages" in loc:
            return "page " + ", ".join(map(str, loc["pages"]))
        if "slides" in loc:
            return "slide " + ", ".join(map(str, loc["slides"]))
        if "sheet" in loc:
            return f"sheet '{loc['sheet']}'"
        return ""


def search(query: str, k: int | None = None, embedder: Embedder | None = None) -> list[Hit]:
    embedder = embedder or get_embedder()
    k = k or settings.search_top_k
    qvec = embedder.embed_query(query)
    dims = embedder.dims
    with connect() as conn:
        conn.execute(f"SET LOCAL hnsw.ef_search = {int(settings.search_ef)}")
        rows = conn.execute(
            f"""SELECT 1 - ((e.embedding::vector({dims})) <=> %(q)s::vector({dims})) AS score,
                       c.chunk_id, c.doc_id, c.version, d.file_name, c.heading_path, c.location,
                       c.chunk_type, c.content
                FROM embeddings e
                JOIN chunks c    ON c.embed_hash = e.embed_hash
                JOIN documents d ON d.doc_id = c.doc_id AND d.version = c.version
                WHERE e.embed_model = %(model)s AND c.is_active AND d.deleted_at IS NULL
                ORDER BY (e.embedding::vector({dims})) <=> %(q)s::vector({dims})
                LIMIT %(k)s""",
            {"q": qvec, "model": embedder.name, "k": k}).fetchall()
    return [Hit(round(float(r[0]), 3), *r[1:]) for r in rows]


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.search")
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=settings.search_top_k, help="how many results")
    ap.add_argument("--full", action="store_true", help="show the whole chunk text")
    args = ap.parse_args(argv)

    from indexing.db import can_connect
    if not can_connect():
        print("Database not reachable: start it with  docker compose up -d")
        return 1
    hits = search(args.query, args.k)
    if not hits:
        print("No results (is anything indexed? try: python -m ingestion.run --file <file>)")
        return 0
    print(f'\nQ: "{args.query}"\n')
    for i, h in enumerate(hits, 1):
        print(f"{i}. [{h.score:.3f}] {h.file_name} > {h.heading_path.split(' > ', 1)[-1]}"
              f"{'  (' + h.where() + ')' if h.where() else ''}")
        text = h.content if args.full else (h.content[:220] + (" ..." if len(h.content) > 220 else ""))
        print("   " + text.replace("\n", "\n   ") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
