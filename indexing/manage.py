"""
Index management commands.

  python -m indexing.manage sync [--folder PATH] [--dry-run]   keep the index matching a folder
  python -m indexing.manage list                               what's in the index
  python -m indexing.manage delete DOC_ID                      remove a document from search
  python -m indexing.manage approve DOC_ID                     make a needs_review document searchable
  python -m indexing.manage cleanup [--days N]                 hard-delete old deleted data
  python -m indexing.manage access                             who may see what; documents nobody can see (unlabelled)
  python -m indexing.manage set-access DOC_ID --roles hr,finance [--tenant acme]   label one document by hand (survives new versions)
  python -m indexing.manage set-access DOC_ID --unlabel        hide one document from everyone, at once
  python -m indexing.manage apply-rules                        re-apply access_rules.json to every document not labelled by hand
  python -m indexing.manage create-user NAME --tenant acme [--roles hr,finance]   add a person (asks for the password; it is never shown or stored)
  python -m indexing.manage users                              who can sign in, in which company, with which roles
  python -m indexing.manage set-roles NAME --roles hr          change a person's roles (takes effect on their next request)
  python -m indexing.manage set-password NAME                  new password (all their tokens stop working)
  python -m indexing.manage disable-user NAME | enable-user NAME   (disabling also stops tokens at once)
  python -m indexing.manage audit [--user NAME] [--limit N]    who asked for what (ids and counts only, never the question text)
"""
import argparse
import getpass
import sys
from pathlib import Path

from indexing import access, sync, users
from indexing.db import connect
from ingestion.config import settings


def _ask_password() -> str:
    """Asked on the keyboard (not shown, not in the command line, not in the shell history)."""
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat the password: ") != first:
        raise users.UserError("the two passwords are not the same")
    return first


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
    sub.add_parser("access")
    sa = sub.add_parser("set-access")
    sa.add_argument("doc_id")
    sa.add_argument("--roles", help="comma-separated roles, or: all")
    sa.add_argument("--tenant", default="acme")
    sa.add_argument("--unlabel", action="store_true", help="hide the document from everyone")
    sub.add_parser("apply-rules")
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--tenant", required=True)
    cu.add_argument("--roles", default="", help="comma-separated; empty = only documents labelled for everyone")
    sub.add_parser("users")
    au = sub.add_parser("audit")
    au.add_argument("--user", help="username")
    au.add_argument("--limit", type=int, default=20)
    sr = sub.add_parser("set-roles")
    sr.add_argument("username")
    sr.add_argument("--roles", default="")
    for name in ("set-password", "disable-user", "enable-user"):
        sub.add_parser(name).add_argument("username")
    args = ap.parse_args(argv)
    if args.cmd == "set-access" and not args.unlabel and not args.roles:
        ap.error("set-access needs --roles (or --unlabel)")

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
        if not args.dry_run:
            hidden = access.unlabelled_documents()
            if hidden:
                print(f"\n  NO ACCESS LABEL: {len(hidden)} document(s) are invisible to everyone until labelled "
                      f"({', '.join(n for _, n in hidden[:5])}{'...' if len(hidden) > 5 else ''}).\n"
                      "  Add a rule to access_rules.json and run:  python -m indexing.manage apply-rules   (or: set-access DOC_ID --roles ...)")
    elif args.cmd == "list":
        rows = sync.list_documents()
        print(f"{'FILE':32} {'VER':>3}  {'STATUS':13} {'SEARCHABLE':18} {'CHUNKS':>6}  {'ACCESS':26} DOC_ID")
        for doc_id, v, name, status, active, chunks, deleted, label in rows:
            searchable = "yes" if active else (f"deleted {deleted}" if deleted else "no")
            print(f"{name[:32]:32} {v:>3}  {status:13} {searchable:18} {chunks or '':>6}  {label[:26]:26} {doc_id}")
    elif args.cmd == "delete":
        print("deleted (hidden from search)" if sync.delete_document(args.doc_id) else "not found / already deleted")
    elif args.cmd == "approve":
        r = sync.approve_document(args.doc_id)
        print(f"approved: {r.chunks} chunks now searchable" if r.indexed else f"not indexed: {r.reason}")
    elif args.cmd == "cleanup":
        print(sync.cleanup(args.days))
    elif args.cmd == "access":
        try:
            rules = access.load_rules()
        except access.AccessError as e:
            print(f"access_rules.json is INVALID: {e}")
            return 1
        path = Path(settings.access_rules_path)
        print(f"rules: {len(rules)} in {path.name}" + ("" if path.exists() else "  (FILE MISSING: every document stays invisible)"))
        for r in rules:
            if access.is_catch_all(r.pattern):
                print(f"  WARNING: rule '{r.pattern}' matches EVERY file: a forgotten label would publish a document to {', '.join(r.roles)}")
        rows = access.access_report()
        print(f"\n{'FILE':34} {'TENANT':10} {'WHO MAY SEE IT':28} {'SOURCE':8} DOC_ID")
        for name, doc_id, tenant, roles, status, source in sorted(rows):
            who = "UNLABELLED - nobody" if status == "unlabelled" else ("everyone" if roles == ["all"] else ", ".join(roles))
            print(f"{name[:34]:34} {tenant:10} {who:28} {source:8} {doc_id}")
        hidden = [r for r in rows if r[4] == "unlabelled"]
        print(f"\n{len(rows)} documents, {len(rows) - len(hidden)} labelled, {len(hidden)} unlabelled (invisible to everyone)")
    elif args.cmd == "set-access":
        try:
            r = access.set_access(args.doc_id, args.tenant, args.roles or "", unlabel=args.unlabel)
        except access.AccessError as e:
            print(f"not changed: {e}")
            return 1
        print(f"{r['doc_id']}: {r['label']}  ({r['versions']} version(s) updated, no re-indexing needed)")
    elif args.cmd == "apply-rules":
        try:
            c = access.apply_rules()
        except access.AccessError as e:
            print(f"nothing changed: access_rules.json is INVALID: {e}")
            return 1
        print(f"labelled {c['labelled']} | re-labelled {c['relabelled']} | hidden (no rule any more) {c['hidden']} | unchanged {c['unchanged']} | kept (set by hand) {c['kept_manual']}")
    elif args.cmd == "audit":
        from indexing import audit
        uid = None
        if args.user:
            with connect() as conn:
                row = conn.execute("SELECT user_id FROM users WHERE username = %s", (args.user.strip().lower(),)).fetchone()
            if not row:
                print(f"not done: no user '{args.user}'")
                return 1
            uid = row[0]
        rows = audit.recent(args.limit, uid)
        print(f"{'WHEN':19} {'EVENT':14} {'USER':16} {'TENANT':8} {'STATUS':16} {'DOCS':>4} {'CITED':>5} {'CHARS':>5}  REQUEST")
        for at, event, user, tenant, status, docs, chunks, chars, ms, rid, subject in rows:
            who = user or (f"name#{subject[:8]}" if subject else "-")
            print(f"{at:19} {event:14} {who:16} {tenant or '-':8} {status or '-':16} {len(docs):>4} {len(chunks):>5} {chars if chars is not None else '-':>5}  {rid}")
        print(f"\n{len(rows)} record(s), newest first")
    elif args.cmd in ("create-user", "users", "set-roles", "set-password", "disable-user", "enable-user"):
        try:
            if args.cmd == "users":
                rows = users.list_users()
                print(f"{'USERNAME':24} {'TENANT':10} {'ROLES':30} STATE")
                for u in rows:
                    print(f"{u.username:24} {u.tenant:10} {', '.join(u.roles) or '(none)':30} {'active' if u.is_active else 'DISABLED'}")
                print(f"\n{len(rows)} user(s)")
            elif args.cmd == "create-user":
                u = users.create_user(args.username, args.tenant, args.roles, _ask_password())
                print(f"created {u.username} ({u.user_id}) in {u.tenant}, roles: {', '.join(u.roles) or 'none'}")
            elif args.cmd == "set-roles":
                u = users.set_roles(args.username, args.roles)
                print(f"{u.username}: roles are now {', '.join(u.roles) or 'none'} (effective on their next request)")
            elif args.cmd == "set-password":
                u = users.set_password(args.username, _ask_password())
                print(f"{u.username}: password changed; every token issued before is now invalid")
            else:
                u = users.set_active(args.username, args.cmd == "enable-user")
                print(f"{u.username}: {'enabled' if u.is_active else 'DISABLED, existing tokens stop working now'}")
        except users.UserError as e:
            print(f"not done: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
