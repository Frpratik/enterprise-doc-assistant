"""
Safety checks (the gate guard). Every check returns a clear reason on failure.

Order matters: cheap checks first, opening the file last.
  1. allowed extension      4. real type (magic bytes) matches the extension
  2. not empty              5. file actually opens (not corrupted / no password / page limit)
  3. not too big            6. zip-bomb guard for Office files
"""
import hashlib
import zipfile
from pathlib import Path

import filetype

from ingestion.config import settings

# extension -> our simple file type name
FILE_TYPES = {
    ".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx", ".csv": "csv",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".tif": "image", ".tiff": "image",
    ".txt": "txt", ".md": "md", ".html": "html", ".htm": "html",
}

# what the REAL content must look like for each extension (from magic bytes)
EXPECTED_REAL_TYPE = {
    ".pdf": {"pdf"}, ".docx": {"docx", "zip"}, ".pptx": {"pptx", "zip"}, ".xlsx": {"xlsx", "zip"},
    ".png": {"png"}, ".jpg": {"jpg"}, ".jpeg": {"jpg"}, ".tif": {"tif"}, ".tiff": {"tif"},
}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".html", ".htm"}

# a file inside every valid Office file
OFFICE_MARKER = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml", ".xlsx": "xl/workbook.xml"}


class SafetyError(Exception):
    """Raised with a human-readable reason when a file must be rejected."""


def sha256(path: Path) -> str:
    """Hash the file in 1 MB pieces (big files never fully loaded into memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_file(path: Path) -> dict:
    """Run all checks. Returns basic info, or raises SafetyError(reason)."""
    path = Path(path)
    ext = path.suffix.lower()

    # 1. allowed type
    if ext not in settings.allowed_extensions:
        raise SafetyError(f"file type '{ext or 'no extension'}' is not allowed")

    # 2 + 3. size
    size = path.stat().st_size
    if size == 0:
        raise SafetyError("file is empty (0 bytes)")
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if size > max_bytes:
        raise SafetyError(f"file is {size / 1024 / 1024:.1f} MB, limit is {settings.max_file_size_mb} MB")

    # 4. real type from the first bytes of the file (catches renamed .exe etc.)
    guess = filetype.guess(str(path))
    mime = guess.mime if guess else None
    if ext in EXPECTED_REAL_TYPE:
        real = guess.extension if guess else "unknown"
        if real not in EXPECTED_REAL_TYPE[ext]:
            raise SafetyError(f"name says '{ext}' but the content is really '{real}'")
    elif ext in TEXT_EXTENSIONS:
        head = path.read_bytes()[:65536]
        if guess is not None or b"\x00" in head:
            raise SafetyError(f"name says '{ext}' (text) but the content is binary")

    # 5 + 6. does it actually open?
    pages = _open_check(path, ext)

    return {
        "file_type": FILE_TYPES[ext],
        "mime_type": mime or ("text/plain" if ext in TEXT_EXTENSIONS else None),
        "size_bytes": size,
        "hash": sha256(path),
        "pages": pages,
    }


def _open_check(path: Path, ext: str):
    """Light 'can we open it?' check per type. Returns page count for PDFs."""
    if ext == ".pdf":
        import pymupdf
        try:
            doc = pymupdf.open(path)
        except Exception:
            raise SafetyError("PDF is corrupted and cannot be opened")
        with doc:
            if doc.needs_pass:
                raise SafetyError("PDF is password-protected")
            if doc.page_count == 0:
                raise SafetyError("PDF has no pages")
            if doc.page_count > settings.max_pages:
                raise SafetyError(f"PDF has {doc.page_count} pages, limit is {settings.max_pages}")
            return doc.page_count

    if ext in OFFICE_MARKER:
        if not zipfile.is_zipfile(path):
            raise SafetyError(f"{ext} file is corrupted (not a valid Office file)")
        with zipfile.ZipFile(path) as z:
            if OFFICE_MARKER[ext] not in z.namelist():
                raise SafetyError(f"{ext} file is corrupted (missing {OFFICE_MARKER[ext]})")
            unpacked = sum(i.file_size for i in z.infolist())
            if unpacked > settings.max_file_size_mb * 1024 * 1024 * 20:   # zip-bomb guard
                raise SafetyError("Office file unpacks to a suspiciously huge size")
        return None

    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        from PIL import Image
        try:
            with Image.open(path) as img:
                img.verify()
        except Exception:
            raise SafetyError("image is corrupted and cannot be opened")
        return 1

    return None   # text files: already checked above
