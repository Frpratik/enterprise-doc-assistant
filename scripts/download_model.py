"""
Download the local embedding model once (about 70 MB) into the project's models/ folder.

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
