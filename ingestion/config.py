"""
All settings in ONE place (no hard-coded values in the pipeline).

- Defaults live here.
- Any value can be overridden in .env with the prefix INGEST_
  e.g. INGEST_MAX_FILE_SIZE_MB=100
- Paths use pathlib, so they work on Windows and Linux.
"""
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = the folder that contains the "ingestion" package
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INGEST_",
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
    )

    # ---------- Folders ----------
    data_dir: Path = PROJECT_ROOT / "data"
    inbox_dir: Path = PROJECT_ROOT / "data" / "inbox"          # drop files here
    originals_dir: Path = PROJECT_ROOT / "data" / "originals"  # untouched copies (source of truth)
    output_dir: Path = PROJECT_ROOT / "data" / "output"        # one JSON per document
    failed_dir: Path = PROJECT_ROOT / "data" / "failed"        # files that failed + reason
    manifest_path: Path = PROJECT_ROOT / "data" / "manifest.json"  # status register
    log_path: Path = PROJECT_ROOT / "data" / "logs" / "ingestion.jsonl"  # one JSON line per event

    # ---------- Safety limits (guardrails) ----------
    max_file_size_mb: int = 50
    max_pages: int = 500
    allowed_extensions: set[str] = {
        ".pdf", ".docx", ".pptx", ".xlsx", ".csv",
        ".png", ".jpg", ".jpeg", ".tif", ".tiff",
        ".txt", ".md", ".html", ".htm",
    }

    # ---------- Intake ----------
    file_ready_wait_sec: float = 2.0   # wait until a file stops growing (still copying?)

    # ---------- OCR ----------
    tesseract_cmd: Optional[str] = None  # Windows: path to tesseract.exe if not on PATH
    ocr_lang: str = "eng"
    ocr_timeout_sec: int = 30            # per page / per image
    ocr_dpi: int = 300                   # render scanned PDF pages at this quality

    # ---------- Parsing ----------
    table_rows_per_element: int = 100     # big sheets are split into tables of this many rows
    min_image_px: int = 50                # ignore tiny images (icons, lines) smaller than this

    # ---------- Detection (per PDF page) ----------
    pdf_min_text_chars: int = 20          # fewer real characters than this = no usable text layer
    pdf_min_image_coverage: float = 0.01  # images covering >1% of the page count as "has images"

    # ---------- Quality thresholds ----------
    min_chars_per_page: int = 50
    min_ocr_confidence: float = 0.70
    max_garbage_ratio: float = 0.30
    min_page_coverage: float = 0.80

    # ---------- Database (Phase 2) ----------
    # read from DATABASE_URL (standard name) or INGEST_DATABASE_URL
    database_url: str = Field(
        default="postgresql://eda:eda_local_pw@localhost:5432/eda",
        validation_alias=AliasChoices("DATABASE_URL", "INGEST_DATABASE_URL"),
    )

    # ---------- Embeddings (Phase 2) ----------
    embed_provider: str = "local"                  # "local" (free, offline) or "openai"
    embed_model: str = "BAAI/bge-small-en-v1.5"   # local, free; reads max 512 tokens at once
    models_dir: Path = PROJECT_ROOT / "models"     # downloaded model files live here (not in git)
    embed_batch_size: int = 64                     # texts per call
    # BGE models work best when SEARCH QUESTIONS (not documents) start with this instruction
    embed_query_prefix: str = "Represent this sentence for searching relevant passages: "
    openai_embed_model: str = "text-embedding-3-small"
    openai_api_key: Optional[str] = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY"))

    # ---------- Chunking (Phase 2) ----------
    chunk_target_tokens: int = 350   # aim for about this size
    chunk_max_tokens: int = 480      # hard limit (the model cuts off anything above 512)
    chunk_overlap_tokens: int = 40   # only used when one long section must be split

    # ---------- Indexing (Phase 2) ----------
    use_database: bool = True          # False = Phase 1 only (JSON manifest, no indexing)
    index_needs_review: bool = False   # True = also index documents flagged needs_review (not approved yet)
    source_dir: Path = PROJECT_ROOT / "data" / "source"   # default folder that `sync` keeps the index matching
    keep_deleted_days: int = 30        # cleanup: hard-delete soft-deleted docs / old versions after this many days
    search_top_k: int = 5
    search_ef: int = 100               # HNSW search effort: higher = more accurate, a bit slower (default 40)
    search_mode: str = "hybrid"        # "hybrid" (vectors + keywords), "vector" or "keyword"
    search_candidates: int = 20        # how many results each of the two searches contributes
    rrf_keyword_weight: float = 0.1    # how much the keyword list counts vs the vector list (1.0 = equal). 0.1 = vector leads, keywords rescue exact terms
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"   # local cross-encoder (~80 MB, English)
    rerank_candidates: int = 10        # how many search results the reranker reads closely (more = slower, ~50 ms each)
    min_rerank_score: float = -10.0    # "I don't know" gate: block if the best rerank score is below this ...
    min_vector_score: float = 0.56     # ... or the best meaning score (cosine) is below this. Re-check with indexing.abstain_eval
    rrf_k: int = 60                    # RRF constant: how quickly lower ranks lose weight (60 is the usual value)

    # ---------- Background jobs (Phase 2) ----------
    celery_broker_url: str = "redis://localhost:6379/0"      # the job queue (Redis from docker-compose)
    celery_result_backend: str = "redis://localhost:6379/1"  # where job status/results are kept
    nightly_sync_hour: int = 2                                # Celery Beat runs the folder sync at 02:15
    nightly_sync_minute: int = 15
    job_time_limit_sec: int = 600                             # a job running longer than this is stopped

    # ---------- LLM (Phase 3 Stage 4): Groq, with a fallback chain ----------
    groq_api_key: str = Field("", validation_alias=AliasChoices("GROQ_API_KEY", "INGEST_GROQ_API_KEY"))  # in .env, never in git
    llm_main_model: str = "qwen/qwen3.8-27b"          # writes the answers (first word in ~160 ms; chosen after the Stage 5 timings)
    llm_fallback_model: str = "openai/gpt-oss-120b"   # a DIFFERENT model family: used when the main model fails
    llm_small_model: str = "openai/gpt-oss-20b"       # simple, cheap jobs; also the last fallback
    llm_timeout_sec: int = 30                          # one call must finish within this
    llm_max_wait_sec: float = 5.0                      # on a rate limit, wait at most this long before trying the next model
    llm_answer_max_tokens: int = 1200                  # reasoning models "think" first, so leave room
    answer_context_chunks: int = 5                     # how many chunks the LLM gets to read

    # ---------- Versioning ----------
    parser_version: str = "1.0.0"   # bump when parsing logic changes -> lets us re-process old docs

    def ensure_dirs(self) -> None:
        """Create the data folders if they don't exist."""
        for d in (self.inbox_dir, self.originals_dir, self.output_dir, self.failed_dir, self.log_path.parent):
            d.mkdir(parents=True, exist_ok=True)


# One shared settings object for the whole app
settings = Settings()
