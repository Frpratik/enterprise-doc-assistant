"""
The command you run.

  python -m ingestion.run --file "C:\\path\\to\\file.pdf"      one or more files (never changed, only copied)
  python -m ingestion.run --folder "C:\\path\\to\\folder"      every file in a folder (copied)
  python -m ingestion.run --watch                              process data/inbox, then keep watching it
  options: --summary  (only the summary table)   --max-chars 300  (shorten long text in the printout)
"""
import argparse
import queue
import sys
import time
from pathlib import Path

from ingestion.config import settings
from ingestion.manifest import Manifest
from ingestion.pipeline import process_file
from ingestion.printer import print_document, print_summary


def main(argv=None) -> int:
    # Windows consoles: print any character (Hindi, symbols) instead of crashing
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="python -m ingestion.run", description="Document ingestion pipeline")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", nargs="+", help="one or more files")
    group.add_argument("--folder", help="process every file in this folder")
    group.add_argument("--watch", action="store_true", help="watch the inbox folder")
    ap.add_argument("--summary", action="store_true", help="only print the summary table")
    ap.add_argument("--max-chars", type=int, default=0, help="shorten long text in the printout")
    args = ap.parse_args(argv)

    settings.ensure_dirs()
    manifest = Manifest(settings.manifest_path)

    if args.watch:
        return watch(manifest, args)

    if args.file:
        paths = [Path(p) for p in args.file]
    else:
        folder = Path(args.folder)
        if not folder.is_dir():
            print(f"Folder not found: {folder}")
            return 2
        paths = sorted(p for p in folder.iterdir() if p.is_file())

    docs = []
    for path in paths:
        if not path.is_file():
            print(f"File not found: {path}")
            continue
        doc = process_file(path, manifest, move=False)       # user's files: copy only
        if doc is None:
            continue
        docs.append(doc)
        if not args.summary:
            print_document(doc, args.max_chars)
    print_summary(docs)
    return 0 if docs and all(d.status.value != "failed" for d in docs) else 1


def watch(manifest: Manifest, args) -> int:
    """Process what's already in the inbox, then process new files as they arrive."""
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    todo: "queue.Queue[Path]" = queue.Queue()

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                todo.put(Path(event.src_path))

        def on_moved(self, event):                     # e.g. browser renames .crdownload -> .pdf
            if not event.is_directory:
                todo.put(Path(event.dest_path))

    for p in sorted(settings.inbox_dir.iterdir()):
        if p.is_file() and p.name != ".gitkeep":
            todo.put(p)

    observer = Observer()
    observer.schedule(Handler(), str(settings.inbox_dir), recursive=False)
    observer.start()
    print(f"Watching {settings.inbox_dir}  (drop files here, Ctrl+C to stop)\n")
    try:
        while True:
            try:
                path = todo.get(timeout=1)
            except queue.Empty:
                continue
            if not path.exists() or path.name == ".gitkeep":   # already handled (events can repeat)
                continue
            doc = process_file(path, manifest, move=True)      # inbox files: moved out after processing
            if doc is not None:
                print_document(doc, args.max_chars) if not args.summary else print_summary([doc])
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        observer.stop()
        observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
