"""
Download the local models once: the embedding model (about 70 MB) and the reranker (about 80 MB) into the project's models/ folder.

Run:  python scripts/download_model.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastembed import TextEmbedding  # noqa: E402

from ingestion.config import settings  # noqa: E402

print(f"Downloading {settings.embed_model} into {settings.models_dir} ...")
start = time.time()
model = TextEmbedding(settings.embed_model, cache_dir=str(settings.models_dir))
vec = next(iter(model.embed(["Employees get 12 sick leaves per year."])))
print(f"[OK] model ready in {time.time() - start:.1f}s  -  one sentence -> {len(vec)} numbers")
tok = list(settings.models_dir.rglob("tokenizer.json"))
print(f"[OK] tokenizer found: {tok[0] if tok else 'NOT FOUND'}")

# ---- the reranker (Phase 3): reads a question + a chunk TOGETHER and says how well the chunk answers it ----
from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: E402

print(f"\nDownloading reranker {settings.rerank_model} ...")
start = time.time()
reranker = TextCrossEncoder(settings.rerank_model, cache_dir=str(settings.models_dir))
good, bad = list(reranker.rerank("how many sick leaves", ["Employees get 12 sick leaves per year.", "Free lunch on Fridays."]))
print(f"[OK] reranker ready in {time.time() - start:.1f}s  -  relevant chunk {good:.2f}, unrelated chunk {bad:.2f}")
