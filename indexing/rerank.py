"""
Reranker = a second, closer look at the best search results.

  Search (vectors + keywords) is FAST but shallow: the question and each chunk are turned into
  numbers SEPARATELY, then compared. A cross-encoder reads the question and ONE chunk TOGETHER, word
  by word, and says how well the chunk answers the question. That is slower (so we only do it for the
  ~20 best candidates) but much more precise.

  Analogy: search = a librarian fetching 20 books by their covers; the reranker = an expert opening each
  book and reading the page that matters.

Model: Xenova/ms-marco-MiniLM-L-6-v2, local and free (about 80 MB; python scripts/download_model.py).
Scores are raw model outputs, NOT 0-1: roughly +5 or more = clearly relevant, below 0 = probably not.
"""
import threading

from ingestion.config import settings

_model = None
_lock = threading.Lock()


class RerankerNotInstalled(RuntimeError):
    pass


def _load():
    global _model
    with _lock:
        if _model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            slug = "models--" + settings.rerank_model.replace("/", "--")
            if not (settings.models_dir / slug).exists():
                raise RerankerNotInstalled(
                    f"reranker '{settings.rerank_model}' is not downloaded. Run:  python scripts\\download_model.py")
            _model = TextCrossEncoder(settings.rerank_model, cache_dir=str(settings.models_dir), local_files_only=True)
    return _model


def rerank_text(hit) -> str:
    """What the reranker reads for a hit: the heading path (document > section) + the chunk text."""
    return f"{hit.heading_path}\n{hit.content}" if hit.heading_path else hit.content


def score_pairs(query: str, texts: list[str]) -> list[float]:
    """One relevance score per text (higher = better answer to the query). Same order as `texts`."""
    if not texts:
        return []
    return [float(x) for x in _load().rerank(query, texts)]


def rerank(query: str, hits: list, k: int | None = None) -> list:
    """Re-order `hits` by how well each one answers `query`; keep the best k. Sets hit.rerank_score and hit.score."""
    scores = score_pairs(query, [rerank_text(h) for h in hits])
    for h, s in zip(hits, scores):
        h.rerank_score = round(s, 3)
        h.score = h.rerank_score
    ordered = sorted(hits, key=lambda h: (-h.rerank_score, -h.vector_score))
    return ordered[:k] if k else ordered
