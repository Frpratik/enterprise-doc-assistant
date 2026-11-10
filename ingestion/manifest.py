"""
Manifest = the register book of every file the pipeline has seen.

It answers 3 questions for the gate:
  1. Have we already processed these exact bytes?  (hash -> duplicate)
  2. Have we seen this file NAME before?            (-> new version of same doc)
  3. What happened to each file?                    (status + reason)

Phase 1 keeps it as one JSON file. Phase 2 moves it into Postgres.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ingestion.models import Document, Status

# a file with one of these statuses "counts" as already ingested
ACTIVE = {Status.QUEUED.value, Status.PROCESSING.value, Status.DONE.value, Status.NEEDS_REVIEW.value}


class Manifest:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.records: dict[str, dict] = {}   # key = "doc_id:v<version>"
        if self.path.exists():
            self.records = json.loads(self.path.read_text(encoding="utf-8")).get("records", {})

    # ---------- questions ----------
    def find_by_hash(self, file_hash: str) -> Optional[dict]:
        """Same bytes already ingested? (failed attempts don't count, they can be retried)"""
        for r in self.records.values():
            if r["hash"] == file_hash and r["status"] in ACTIVE:
                return r
        return None

    def latest_by_name(self, file_name: str) -> Optional[dict]:
        """Newest version of a document with this file name (case-insensitive)."""
        same = [r for r in self.records.values()
                if r["file_name"].lower() == file_name.lower() and r["status"] in ACTIVE]
        return max(same, key=lambda r: r["version"]) if same else None

    # ---------- write ----------
    def save(self, doc: Document) -> None:
        key = f"{doc.doc_id}:v{doc.version}"
        self.records[key] = {
            "doc_id": doc.doc_id,
            "version": doc.version,
            "file_name": doc.file_name,
            "hash": doc.hash,
            "status": doc.status.value,
            "status_reason": doc.status_reason,
            "original_path": doc.original_path,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._write()

    def _write(self) -> None:
        # write to a temp file first, then swap -> the manifest is never half-written
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"records": self.records}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
