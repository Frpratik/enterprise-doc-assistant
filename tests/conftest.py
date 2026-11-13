"""Shared test setup: make sure sample files exist, and use a temp data folder per test."""
import subprocess
import sys
from pathlib import Path

import pytest

from ingestion.config import settings

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"


@pytest.fixture(scope="session")
def samples() -> Path:
    if not (SAMPLES / "good").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_data.py")], check=True)
    return SAMPLES


@pytest.fixture
def temp_data(tmp_path, monkeypatch):
    """Point all pipeline folders at a temp folder so tests never touch real data."""
    for name in ("inbox_dir", "originals_dir", "output_dir", "failed_dir"):
        monkeypatch.setattr(settings, name, tmp_path / name.replace("_dir", ""))
    monkeypatch.setattr(settings, "manifest_path", tmp_path / "manifest.json")
    monkeypatch.setattr(settings, "file_ready_wait_sec", 0.01)
    monkeypatch.setattr(settings, "use_database", False)    # Phase 1 tests never touch a database
    settings.ensure_dirs()
    return tmp_path


# ---------------- Phase 2: a real Postgres for tests ----------------
import os
import uuid

import psycopg
from psycopg.conninfo import make_conninfo


@pytest.fixture(scope="session")
def pg_server_url(tmp_path_factory):
    """
    A Postgres server for tests, in this order:
      1. TEST_DATABASE_URL if set
      2. the normal DATABASE_URL (your Docker Postgres)
      3. pgserver (a pip-installed Postgres with pgvector), if installed
    Tests are skipped if none is available.
    """
    for url in (os.environ.get("TEST_DATABASE_URL"), settings.database_url):
        if url:
            try:
                with psycopg.connect(url, connect_timeout=2):
                    return url
            except Exception:
                pass
    try:
        import pgserver
    except ImportError:
        pytest.skip("no Postgres for tests: start Docker (docker compose up -d)")
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    return srv.get_uri()


@pytest.fixture
def db_url(pg_server_url, monkeypatch):
    """A brand-new empty database for ONE test, dropped afterwards. Your real data is never touched."""
    from indexing import db
    from indexing.migrate import migrate

    name = f"eda_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(pg_server_url, dbname=name)
    monkeypatch.setattr(settings, "database_url", url)
    migrate(url, quiet=True)
    yield url
    db.close_all()
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ---------------- logs: tests must never write into the real log file ----------------
@pytest.fixture(autouse=True)
def _logs_go_to_a_temp_file(tmp_path, monkeypatch):
    """Every test logs into its own temp file. Before this, test runs added hundreds of lines
    (including fake error messages) to data/logs/ingestion.jsonl."""
    monkeypatch.setattr(settings, "log_path", tmp_path / "test_logs" / "ingestion.jsonl")
    from ingestion.logs import get_logger
    get_logger()        # structlog only switches files when get_logger() is called: do it now
