"""
Submit background jobs and check on them.

  python -m indexing.jobs submit FILE [FILE ...]     returns a job id per file, immediately
  python -m indexing.jobs status JOB_ID [JOB_ID ...]
  python -m indexing.jobs sync [--folder PATH]        sync a folder in the background
"""
import argparse
import sys
from pathlib import Path

from ingestion.config import settings


def describe(job_id: str) -> str:
    from indexing.worker import app
    r = app.AsyncResult(job_id)
    if r.state == "SUCCESS":
        v = r.result
        if "chunks" in v:
            detail = (f"{v['status']}, {v['chunks']} chunks searchable ({v['embeddings_new']} new, "
                      f"{v['embeddings_reused']} reused)" if v["indexed"]
                      else f"{v['status']}, not indexed: {v['index_note'] or v['reason']}")
            return f"DONE      {v['file']}: {detail}"
        return f"DONE      {v}"
    if r.state == "PROGRESS":
        return f"RUNNING   {r.info}"
    if r.state == "FAILURE":
        return f"FAILED    {type(r.result).__name__}: {r.result}"
    return {"PENDING": "WAITING   (in the queue, or unknown id)", "STARTED": "RUNNING",
            "RETRY": "RETRYING  (temporary problem, will try again)"}.get(r.state, r.state)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("files", nargs="+")
    st = sub.add_parser("status")
    st.add_argument("job_ids", nargs="+")
    sy = sub.add_parser("sync")
    sy.add_argument("--folder", default=str(settings.source_dir))
    args = ap.parse_args(argv)

    from indexing.worker import ingest_file, sync_folder
    try:
        if args.cmd == "submit":
            for f in args.files:
                p = Path(f).resolve()
                if not p.is_file():
                    print(f"not found: {f}")
                    continue
                job = ingest_file.delay(str(p), False)
                print(f"queued  {job.id}  {p.name}")
            print("\ncheck with:  python -m indexing.jobs status <job id>")
        elif args.cmd == "sync":
            job = sync_folder.delay(str(Path(args.folder).resolve()))
            print(f"queued  {job.id}  sync {args.folder}")
        else:
            for j in args.job_ids:
                print(f"{j}  {describe(j)}")
    except Exception as e:                   # Redis not running
        print(f"Cannot reach the job queue ({type(e).__name__}). Is Docker running?  docker compose up -d")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
