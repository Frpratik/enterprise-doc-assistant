"""
Background worker (Celery + Redis).

  waiter  = whoever submits a job (CLI, watcher, later the API): hands over a ticket, free at once
  board   = Redis: jobs wait here
  cook    = the Celery worker: parses, OCRs, embeds and indexes in the background
  alarm   = Celery Beat: starts the folder sync every night

Start them (Docker's Redis must be running):
  celery -A indexing.worker worker --loglevel=info --pool=solo     (--pool=solo is needed on Windows)
  celery -A indexing.worker beat --loglevel=info                   (only for the nightly sync)

Submit / check jobs:  python -m indexing.jobs submit FILE...  |  status JOB_ID  |  sync
"""
from pathlib import Path

import psycopg
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_init

from ingestion.config import settings

app = Celery("eda", broker=settings.celery_broker_url, backend=settings.celery_result_backend)
app.conf.update(
    task_acks_late=True,                 # a job is removed from the queue only AFTER it finishes:
                                         # if the worker crashes mid-job, the job goes back to the queue
    worker_prefetch_multiplier=1,        # take one job at a time (jobs are slow and uneven)
    task_track_started=True,             # status shows STARTED while running, not just PENDING
    task_time_limit=settings.job_time_limit_sec,
    task_soft_time_limit=settings.job_time_limit_sec - 30,
    result_expires=60 * 60 * 24 * 7,     # keep job results for 7 days
    broker_connection_retry_on_startup=True,
    timezone="Asia/Kolkata",
    beat_schedule={
        "nightly-folder-sync": {
            "task": "indexing.worker.sync_folder",
            "schedule": crontab(hour=settings.nightly_sync_hour, minute=settings.nightly_sync_minute),
            "args": [str(settings.source_dir)],
        },
    },
)


@worker_init.connect
def check_database_before_starting(**_):
    """
    Fail FAST at startup instead of failing every job later:
    the worker refuses to start if the database is unreachable or its schema is out of date.
    (Real case: the worker ran against a database missing migration 002, and every job failed with
    'column "indexed_at" does not exist'.)
    """
    from indexing.db import can_connect
    from indexing.migrate import _files, applied

    if not can_connect():
        raise SystemExit("Worker not started: database not reachable. Start it with:  docker compose up -d")
    pending = [f.name for f in _files() if f.name not in applied()]
    if pending:
        raise SystemExit(f"Worker not started: {len(pending)} database migration(s) pending ({', '.join(pending)}).\n"
                         f"Run:  python -m indexing.migrate   then start the worker again.")


# Retry only problems that fix themselves (database briefly down / restarting).
# A broken FILE is not retried: it ends as status 'failed' with a reason, like in Phase 1.
TEMPORARY_ERRORS = (psycopg.OperationalError, ConnectionError, TimeoutError)


def _progress(task, **meta):
    """Show 'PROGRESS' in the job status. Skipped in eager (test) mode: it would write to Redis."""
    if not task.request.is_eager:
        task.update_state(state="PROGRESS", meta=meta)


@app.task(bind=True, name="indexing.worker.ingest_file",
          autoretry_for=TEMPORARY_ERRORS, retry_backoff=5, retry_backoff_max=120, retry_jitter=True,
          max_retries=3)
def ingest_file(self, path: str, move: bool = False) -> dict:
    """Process + index one file. Safe to run twice: the gate skips exact duplicates."""
    from indexing import service
    _progress(self, step="processing", file=Path(path).name)
    doc, idx = service.ingest_and_index(Path(path), move=move)
    if doc is None:
        return {"file": Path(path).name, "status": "ignored", "reason": "temporary file"}
    return {
        "file": doc.file_name, "doc_id": doc.doc_id, "version": doc.version,
        "status": doc.status.value, "reason": doc.status_reason,
        "indexed": bool(idx and idx.indexed),
        "chunks": idx.chunks if idx else 0,
        "embeddings_new": idx.embeddings_new if idx else 0,
        "embeddings_reused": idx.embeddings_cached if idx else 0,
        "index_note": (idx.reason if idx and not idx.indexed else ""),
    }


@app.task(bind=True, name="indexing.worker.sync_folder",
          autoretry_for=TEMPORARY_ERRORS, retry_backoff=10, max_retries=3)
def sync_folder(self, folder: str) -> dict:
    """Keep the index matching a folder (also run every night by Celery Beat)."""
    from indexing.sync import run_sync
    _progress(self, step="syncing", folder=folder)
    plan = run_sync(Path(folder))
    return {"folder": folder, "new": len(plan.new), "changed": len(plan.changed),
            "unchanged": len(plan.unchanged), "renamed": len(plan.renamed), "deleted": len(plan.deleted)}
