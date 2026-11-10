"""
Intake = the front door. For each incoming file:

  1. ignore temp / hidden files (Word's ~$ lock files, browser downloads in progress...)
  2. wait until the file is fully copied (size stops changing)
  3. run the safety checks
  4. duplicate?  -> skip.   same name, new content? -> new version of the same doc
  5. keep an untouched copy in data/originals/<doc_id>/v<version><ext>
  6. record everything in the manifest

Returns a Document with status QUEUED (ready for parsing), FAILED or SKIPPED_DUPLICATE.
"""
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from ingestion.config import settings
from ingestion.manifest import Manifest
from ingestion.models import Document, Status
from ingestion.safety import SafetyError, check_file

TEMP_PREFIXES = ("~$", ".~", "~")                       # Office lock files, hidden files
TEMP_SUFFIXES = (".tmp", ".part", ".crdownload", ".download", ".partial")


def is_temp_file(path: Path) -> bool:
    name = Path(path).name
    return name.startswith(TEMP_PREFIXES) or name.startswith(".") or name.lower().endswith(TEMP_SUFFIXES)


def wait_until_ready(path: Path, max_wait_sec: float = 60) -> bool:
    """A file being copied keeps growing. Wait until its size is stable."""
    path = Path(path)
    waited, last = 0.0, -1
    step = settings.file_ready_wait_sec
    while waited <= max_wait_sec:
        if not path.exists():
            return False
        size = path.stat().st_size
        if size == last:
            return True
        last = size
        time.sleep(step)
        waited += step
    return False


def admit(path: Path, manifest: Manifest, move: bool = False, wait: bool = True) -> Optional[Document]:
    """
    Run the gate for one file.
    move=True  -> file came from the inbox, take it out of the inbox
    move=False -> file is the user's own file, never touch it (copy only)
    Returns None for temp files (silently ignored).
    """
    path = Path(path)
    if is_temp_file(path):
        return None
    if wait and not wait_until_ready(path):
        doc = Document(doc_id=_new_id(), file_name=path.name, source_path=str(path))
        doc.mark(Status.FAILED, "file disappeared or kept changing while waiting for it")
        return doc

    doc = Document(doc_id=_new_id(), file_name=path.name, source_path=str(path))

    # ---- safety checks ----
    try:
        info = check_file(path)
    except SafetyError as e:
        doc.mark(Status.FAILED, str(e))
        _to_failed(path, doc, move)
        manifest.save(doc)
        return doc

    doc.file_type = info["file_type"]
    doc.mime_type = info["mime_type"]
    doc.size_bytes = info["size_bytes"]
    doc.hash = info["hash"]
    doc.metadata.page_count = info["pages"]

    # ---- duplicate? ----
    same = manifest.find_by_hash(doc.hash)
    if same:
        doc.doc_id, doc.version = same["doc_id"], same["version"]
        doc.mark(Status.SKIPPED_DUPLICATE,
                 f"same content as '{same['file_name']}' ({same['doc_id']} v{same['version']})")
        if move:
            path.unlink()            # nothing new to keep
        return doc

    # ---- new version of a known document? ----
    previous = manifest.latest_by_name(doc.file_name)
    if previous:
        doc.doc_id, doc.version = previous["doc_id"], previous["version"] + 1

    # ---- keep the untouched original ----
    target = settings.originals_dir / doc.doc_id / f"v{doc.version}{path.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    (shutil.move if move else shutil.copy2)(str(path), str(target))
    doc.original_path = str(target)

    doc.mark(Status.QUEUED)
    manifest.save(doc)
    return doc


def _new_id() -> str:
    return f"doc_{uuid.uuid4().hex[:12]}"


def _to_failed(path: Path, doc: Document, move: bool) -> None:
    """Put the rejected file in data/failed/ with a .reason.txt next to it."""
    settings.failed_dir.mkdir(parents=True, exist_ok=True)
    target = settings.failed_dir / f"{doc.doc_id}_{path.name}"
    (shutil.move if move else shutil.copy2)(str(path), str(target))
    target.with_name(target.name + ".reason.txt").write_text(doc.status_reason or "", encoding="utf-8")
