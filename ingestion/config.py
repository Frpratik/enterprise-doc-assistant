"""
All settings in ONE place (no hard-coded values in the pipeline).

- Defaults live here.
- Any value can be overridden in .env with the prefix INGEST_
  e.g. INGEST_MAX_FILE_SIZE_MB=100
- Paths use pathlib, so they work on Windows and Linux.
"""
from pathlib import Path
from typing import Optional

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

    # ---------- Quality thresholds ----------
    min_chars_per_page: int = 50
    min_ocr_confidence: float = 0.70
    max_garbage_ratio: float = 0.30
    min_page_coverage: float = 0.80

    # ---------- Versioning ----------
    parser_version: str = "1.0.0"   # bump when parsing logic changes -> lets us re-process old docs

    def ensure_dirs(self) -> None:
        """Create the data folders if they don't exist."""
        for d in (self.inbox_dir, self.originals_dir, self.output_dir, self.failed_dir):
            d.mkdir(parents=True, exist_ok=True)


# One shared settings object for the whole app
settings = Settings()
