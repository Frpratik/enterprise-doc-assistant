"""
Count tokens the way the EMBEDDING MODEL sees them.

The local model (bge-small) reads at most 512 tokens - anything longer is silently cut off.
So chunk sizes are measured with the model's own tokenizer (downloaded with the model).
If the model isn't downloaded yet, a careful estimate is used instead.
"""
import re
from functools import lru_cache

from ingestion.config import settings


@lru_cache(maxsize=1)
def _tokenizer():
    from tokenizers import Tokenizer
    for path in sorted(settings.models_dir.rglob("tokenizer.json")):
        if "bge-small" in str(path).lower() or "bge_small" in str(path).lower():
            return Tokenizer.from_file(str(path))
    return None


def using_real_tokenizer() -> bool:
    return _tokenizer() is not None


def estimate_tokens(text: str) -> int:
    """Rough count: words and symbols x 1.3 (BERT splits numbers, emails and rare words into pieces)."""
    return int(len(re.findall(r"\w+|[^\w\s]", text)) * 1.3) + 2


def count_tokens(text: str) -> int:
    tok = _tokenizer()
    if tok is None:
        return estimate_tokens(text)
    return len(tok.encode(text).ids)
