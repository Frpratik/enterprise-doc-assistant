"""
Retrieve = everything before the LLM: hybrid search -> reranker -> "do we know?" gate.

    r = retrieve("how many sick leaves do I get")
    r.decision.answerable   False -> tell the user we found nothing, do NOT call the LLM
    r.hits                  the best chunks (best first), each with its scores, file and page

Stage 4 (the LLM answer) starts from here.
"""
from dataclasses import dataclass

from indexing.confidence import Decision, decide
from indexing.embeddings import Embedder
from indexing.search import Hit, search
from ingestion.config import settings


@dataclass
class Retrieval:
    question: str
    hits: list[Hit]
    decision: Decision


def retrieve(question: str, k: int | None = None, embedder: Embedder | None = None) -> Retrieval:
    hits = search(question, k=k or settings.search_top_k, embedder=embedder, rerank=True)
    return Retrieval(question, hits, decide(hits))
