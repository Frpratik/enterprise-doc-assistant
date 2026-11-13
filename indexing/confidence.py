"""
The "I don't know" gate: decide, BEFORE calling the LLM, whether search found anything worth answering from.

Why it is needed: search ALWAYS returns its nearest chunks, even for "what is the capital of France?".
If we hand those chunks to the LLM it may build a confident answer from irrelevant text.

How it decides (two signals, EITHER one can say "no"):
  rerank score  the cross-encoder's verdict on the best chunk (raw score, about -11 = unrelated, +5 = clear answer)
  meaning score the best cosine similarity between the question and any retrieved chunk (0 - 1)

What it can and cannot do (measured in Stage 3, see docs/BUILD_LOG.md):
  + stops clearly off-topic questions ("capital of France", "bake a cake")
  - cannot stop NEAR-topic questions ("how much is the joining bonus?") - the documents talk about pay and
    leave, so scores look fine. The LLM step must refuse those when the text does not contain the answer.
  So this gate is deliberately CONSERVATIVE: it must never block a question the documents can answer.

The two thresholds sit in the gap between the weakest answerable question and the strongest off-topic one
in OUR sample corpus. They depend on the models and the corpus: re-check them with
`python -m indexing.abstain_eval` after changing the embedding model, the reranker or the document set.
"""
from dataclasses import dataclass

from ingestion.config import settings

NO_ANSWER_MESSAGE = "I couldn't find anything about this in the company documents."


@dataclass
class Decision:
    answerable: bool
    code: str                    # "ok", "no_results" or "weak_match"
    reason: str                  # a plain-words explanation (for logs; Stage 4 shows NO_ANSWER_MESSAGE to the user)
    top_rerank: float | None = None
    top_vector: float | None = None


def decide(hits: list) -> Decision:
    """Look at the (reranked) search results and say whether the LLM should be asked to answer."""
    if not hits:
        return Decision(False, "no_results", "search found nothing")
    top_vector = max(h.vector_score for h in hits)
    scored = [h.rerank_score for h in hits if h.rerank_score is not None]
    top_rerank = max(scored) if scored else None            # None when the reranker was not used

    if top_rerank is not None and top_rerank < settings.min_rerank_score:
        return Decision(False, "weak_match",
                        f"best rerank score {top_rerank:.1f} is below {settings.min_rerank_score}", top_rerank, top_vector)
    if top_vector < settings.min_vector_score:
        return Decision(False, "weak_match",
                        f"best meaning score {top_vector:.2f} is below {settings.min_vector_score}", top_rerank, top_vector)
    return Decision(True, "ok", "the documents contain a plausible match", top_rerank, top_vector)
