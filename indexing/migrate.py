"""
Database migrations: the schema is versioned like code.

Each file in indexing/migrations/ (001_..., 002_...) runs ONCE, in order, inside a
transaction, and is recorded in the schema_migrations table.

  python -m indexing.migrate            apply new migrations
  python -m indexing.migrate --status   show what is applied / pending
"""
import argparse
import sys
from pathlib import Path

import psycopg

from ingestion.config import settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def applied(url: str | None = None) -> set[str]:
    with psycopg.connect(url or settings.database_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        return {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}


def migrate(url: str | None = None, quiet: bool = False) -> list[str]:
    url = url or settings.database_url
    done = applied(url)
    new = []
    for f in _files():
        if f.name in done:
            continue
        with psycopg.connect(url) as conn:            # one transaction per file: all or nothing
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
        new.append(f.name)
        if not quiet:
            print(f"applied  {f.name}")
    if not quiet and not new:
        print("database is up to date")
    return new


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m indexing.migrate")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.status:
            done = applied()
            for f in _files():
                print(f"{'applied ' if f.name in done else 'PENDING '} {f.name}")
            return 0
        migrate()
        return 0
    except psycopg.OperationalError as e:
        print(f"Cannot connect to the database: {e}\nIs Docker running?  ->  docker compose up -d")
        return 1


if __name__ == "__main__":
    sys.exit(main())
