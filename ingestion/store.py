"""Save results: one JSON file per document version, written safely."""
import os
import shutil

from ingestion.config import settings
from ingestion.models import Document


def save_output(doc: Document) -> str:
    """data/output/<doc_id>_v<version>.json  (old versions are kept, never overwritten)."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    path = settings.output_dir / f"{doc.doc_id}_v{doc.version}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(doc.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    os.replace(tmp, path)            # never a half-written file
    return str(path)


def copy_to_failed(doc: Document) -> None:
    """A file that passed the gate but failed later: keep a copy + the reason next to it."""
    if not doc.original_path:
        return
    settings.failed_dir.mkdir(parents=True, exist_ok=True)
    target = settings.failed_dir / f"{doc.doc_id}_{doc.file_name}"
    shutil.copy2(doc.original_path, target)
    target.with_name(target.name + ".reason.txt").write_text(doc.status_reason or "", encoding="utf-8")
