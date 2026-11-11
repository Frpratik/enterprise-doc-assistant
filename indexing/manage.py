"""
Index management commands.

  python -m indexing.manage sync [--folder PATH] [--dry-run]   keep the index matching a folder
  python -m indexing.manage list                               what's in the index
  python -m indexing.manage delete DOC_ID                      remove a document from search
  python -m indexing.manage approve DOC_ID                     make a needs_review document searchable
  python -m indexing.manage cleanup [--days N]                 hard-delete old deleted data
"""
import argparse
import sys
from pathlib import Path

from indexing import sync
from ingestion.config import settings


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m indexing.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--folder", default=str(settings.source_dir))
    s.add_argument("--dry-run", action="store_true", help="only show what would change")
    sub.add_parser("list")
    d = sub.add_parser("delete")
    d.add_argument("doc_id")
    a = sub.add_parser("approve")
    a.add_argument("doc_id")
    c = sub.add_parser("cleanup")
    c.add_argument("--days", type=int, default=settings.keep_deleted_days)
    args = ap.parse_args(argv)

    from indexing.db import can_connect
    if not can_connect():
        print("Database not reachable: start it with  docker compose up -d")
        return 1

    if args.cmd == "sync":
        folder = Path(args.folder)
        if not folder.is_dir():
            print(f"Folder not found: {folder}")
            return 1

        def show(f, doc, idx):
            what = (f"{idx.chunks} chunks ({idx.embeddings_new} new, {idx.embeddings_cached} reused)"
                    if idx and idx.indexed else (idx.reason if idx else doc.status_reason))
            print(f"  {doc.status.value:18} {f.name:32} {what}")

        plan = sync.run_sync(folder, dry_run=args.dry_run, on_file=None if args.dry_run else show)
        title = "PLAN (dry run, nothing changed)" if args.dry_run else "SYNC DONE"
        print(f"\n{title}  -  {folder}")
        print(f"  new: {len(plan.new)}   changed: {len(plan.changed)}   unchanged: {len(plan.unchanged)}   "
              f"renamed: {len(plan.renamed)}   deleted: {len(plan.deleted)}")
        for doc_id, f in plan.renamed:
            print(f"  renamed  -> {f.name}  ({doc_id})")
        for doc_id, name in plan.deleted:
            print(f"  deleted  {name}  ({doc_id})")
    elif args.cmd == "list":
        rows = sync.list_documents()
        print(f"{'FILE':32} {'VER':>3}  {'STATUS':13} {'SEARCHABLE':18} {'CHUNKS':>6}  DOC_ID")
        for doc_id, v, name, status, active, chunks, deleted in rows:
            searchable = "yes" if active else (f"deleted {deleted}" if deleted else "no")
            print(f"{name[:32]:32} {v:>3}  {status:13} {searchable:18} {chunks or '':>6}  {doc_id}")
    elif args.cmd == "delete":
        print("deleted (hidden from search)" if sync.delete_document(args.doc_id) else "not found / already deleted")
    elif args.cmd == "approve":
        r = sync.approve_document(args.doc_id)
        print(f"approved: {r.chunks} chunks now searchable" if r.indexed else f"not indexed: {r.reason}")
    elif args.cmd == "cleanup":
        print(sync.cleanup(args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
