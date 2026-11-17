"""
Retrieve = everything before the LLM: hybrid search -> reranker -> "do we know?" gate.

    r = retrieve("how many sick leaves do I get")
    r.decision.answerable   False -> tell the user we found nothing, do NOT call the LLM
    r.hits                  the best chunks (best first), each with its scores, file and page

Stage 4 (the LLM answer) starts from here.
"""
from dataclasses import dataclass

from indexing.access import Principal
from indexing import injection
from indexing.confidence import Decision, decide
from indexing.embeddings import Embedder
from indexing.search import Hit, search
from ingestion.config import settings
from ingestion.logs import get_logger

log = get_logger()


@dataclass
class Retrieval:
    question: str
    hits: list[Hit]
    decision: Decision


def drop_hostile(hits: list[Hit]) -> list[Hit]:
    """Remove the chunks that score as prompt injection (a person who approved the document accepted its topic, not its embedded commands)."""
    kept = []
    for h in hits:
        s = injection.scan(h.content)
        if s.hostile:
            log.warning("injection_chunk_dropped", doc_id=h.doc_id, chunk_id=h.chunk_id, categories=s.categories, score=s.score)
        else:
            kept.append(h)
    return kept


def retrieve(question: str, k: int | None = None, embedder: Embedder | None = None, principal: Principal | None = None) -> Retrieval:
    hits = search(question, k=k or settings.search_top_k, embedder=embedder, rerank=True, principal=principal)
    hits = drop_hostile(hits)                    # before the gate and the prompt: a chunk that talks to the model never reaches it
    return Retrieval(question, hits, decide(hits))
