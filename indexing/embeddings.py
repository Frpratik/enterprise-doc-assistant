"""
Embeddings = turn text into a list of numbers (a vector) so that texts with similar MEANING
get similar vectors. Search later = "find the chunks whose vectors are closest to the question's".

One interface, two implementations (chosen in config, the rest of the code never changes):
  LocalEmbedder   BAAI/bge-small-en-v1.5 via fastembed - free, offline, 384 numbers per text
  OpenAIEmbedder  text-embedding-3-small                 - needs an API key, 1536 numbers per text

Documents and search questions are embedded slightly differently:
  embed_documents(texts)  -> chunks, as they are
  embed_query(question)   -> BGE models get an instruction in front of the QUESTION
                             ("Represent this sentence for searching relevant passages: ")

embed_with_cache() puts the database in front of the model: every text is identified by its
SHA-256 hash, and a (hash, model) pair is embedded only ONCE, ever.
"""
import hashlib
from abc import ABC, abstractmethod
from functools import lru_cache

import numpy as np

from ingestion.config import settings


class Embedder(ABC):
    name: str = ""          # stored with every vector, so we always know which model made it
    dims: int = 0

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        ...

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]


def _normalize(v) -> np.ndarray:
    """Length 1, so 'closeness' = dot product = cosine similarity."""
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


class LocalEmbedder(Embedder):
    def __init__(self, model_name: str | None = None):
        from fastembed import TextEmbedding
        self.name = model_name or settings.embed_model
        if not any(settings.models_dir.rglob("tokenizer.json")):
            raise RuntimeError("embedding model not downloaded yet: run  python scripts/download_model.py")
        self._model = TextEmbedding(self.name, cache_dir=str(settings.models_dir), local_files_only=True)
        self.dims = len(self.embed_documents(["dimension check"])[0])

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        if not texts:
            return []
        return [_normalize(v) for v in self._model.embed(texts, batch_size=settings.embed_batch_size)]

    def embed_query(self, text: str) -> np.ndarray:
        prefix = settings.embed_query_prefix if "bge" in self.name.lower() else ""
        return self.embed_documents([prefix + text])[0]


class OpenAIEmbedder(Embedder):
    DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}

    def __init__(self, model_name: str | None = None, client=None):
        self.name = model_name or settings.openai_embed_model
        self.dims = self.DIMS.get(self.name, 0)
        if client is None:
            from openai import OpenAI                       # pip install openai (only needed for this option)
            client = OpenAI(api_key=settings.openai_api_key, max_retries=5, timeout=60)   # retries with backoff
        self._client = client

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        out = []
        for i in range(0, len(texts), 100):                 # up to 100 texts per request
            resp = self._client.embeddings.create(model=self.name, input=texts[i:i + 100])
            out += [_normalize(d.embedding) for d in sorted(resp.data, key=lambda d: d.index)]
        return out


@lru_cache(maxsize=4)
def get_embedder(provider: str | None = None) -> Embedder:
    """Loaded once and reused (loading a model takes seconds)."""
    provider = provider or settings.embed_provider
    if provider == "local":
        return LocalEmbedder()
    if provider == "openai":
        return OpenAIEmbedder()
    raise ValueError(f"unknown embed_provider '{provider}' (use 'local' or 'openai')")


# ---------------- cache in front of the model ----------------
def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_with_cache(conn, texts: list[str], embedder: Embedder | None = None) -> tuple[list[np.ndarray], dict]:
    """
    Vectors for `texts`, in the same order. Only texts never seen before (for this model) go to
    the model; everything else comes from the embeddings table. Duplicates inside `texts` are
    embedded once. Returns (vectors, {'cached': n, 'new': n}).
    """
    embedder = embedder or get_embedder()
    hashes = [text_hash(t) for t in texts]
    unique = list(dict.fromkeys(hashes))

    found = {}
    if unique:
        rows = conn.execute(
            "SELECT embed_hash, embedding FROM embeddings WHERE embed_model = %s AND embed_hash = ANY(%s)",
            (embedder.name, unique),
        ).fetchall()
        found = {h: _to_array(v) for h, v in rows}

    missing = [h for h in unique if h not in found]
    if missing:
        text_of = dict(zip(hashes, texts))
        new_vecs = embedder.embed_documents([text_of[h] for h in missing])
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO embeddings (embed_hash, embed_model, dims, embedding) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                [(h, embedder.name, len(v), v) for h, v in zip(missing, new_vecs)],
            )
        found.update(zip(missing, new_vecs))

    return [found[h] for h in hashes], {"cached": len(unique) - len(missing), "new": len(missing)}


def _to_array(v) -> np.ndarray:
    return v.to_numpy() if hasattr(v, "to_numpy") else np.asarray(v, dtype=np.float32)
