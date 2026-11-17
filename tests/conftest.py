"""Shared test setup: make sure sample files exist, and use a temp data folder per test."""
import subprocess
import sys
from pathlib import Path

import pytest

from ingestion.config import settings

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"


# ---------------- parallel runs (pytest-xdist) ----------------
def pytest_configure(config):
    """The sample documents are made ONCE, by the controller process, before any worker starts: workers that each tried to make them at the same
    moment would read half-written files."""
    settings.embedding_cache_dir = ROOT / "evals" / ".cache" / "embeddings"          # tests embed the same sample documents again and again
    if hasattr(config, "workerinput"):
        return
    for folder, script in (("good", "make_sample_data.py"), ("eval2", "make_eval_corpus.py"), ("access", "make_access_corpus.py"), ("poisoned", "make_poisoned_corpus.py")):
        if not (SAMPLES / folder).exists() or not any((SAMPLES / folder).iterdir()):
            subprocess.run([sys.executable, str(ROOT / "scripts" / script)], check=True, capture_output=True)


def pytest_collection_modifyitems(config, items):
    """Every test that needs the real models is marked `slow` (skip them with: pytest -m "not slow")."""
    for item in items:
        for m in item.iter_markers("skipif"):
            if "models not downloaded" in str(m.kwargs.get("reason", "")):
                item.add_marker(pytest.mark.slow)
                break


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


@pytest.fixture(scope="session")
def migrated_template(pg_server_url):
    """A database with every migration applied, made ONCE per run. Each test then gets a copy (CREATE DATABASE ... TEMPLATE), which takes a
    third of the time of creating an empty database and running all the migrations again."""
    from indexing.migrate import migrate

    name = f"eda_tpl_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    migrate(make_conninfo(pg_server_url, dbname=name), quiet=True)
    yield name
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db_url(pg_server_url, migrated_template, monkeypatch):
    """A brand-new database (all migrations applied) for ONE test, dropped afterwards. Your real data is never touched."""
    from indexing import db

    name = f"eda_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{migrated_template}"')
    url = make_conninfo(pg_server_url, dbname=name)
    monkeypatch.setattr(settings, "database_url", url)
    yield url
    db.close_all()
    with psycopg.connect(pg_server_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ---------------- access (Phase 5): old tests are about search quality, not access ----------------
@pytest.fixture(autouse=True)
def _tests_default_to_an_open_company(tmp_path, monkeypatch):
    """
    By default a test indexes documents under a catch-all rule (acme / everyone) and searches as an acme auditor, so the older
    tests keep measuring what they measured. Production code has NO such default: search(principal=None) raises (tested in test_access_filter.py,
    through search.__wrapped__). A test that is about access sets its own rules file and passes its own principal.
    """
    import functools
    import json

    from indexing import access, search as search_mod
    rules = tmp_path / "default_test_rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [{"match": "*", "tenant": "acme", "roles": ["all"]}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "access_rules_path", rules)

    original = search_mod.search

    @functools.wraps(original)
    def with_default_asker(*args, **kw):
        if kw.get("principal") is None:
            kw["principal"] = access.Principal.auditor("acme")
        return original(*args, **kw)

    for module in list(sys.modules.values()):                     # every module that imported `search` by name
        if getattr(module, "search", None) is original:
            monkeypatch.setattr(module, "search", with_default_asker)


# ---------------- rate limits (Phase 6): older tests are not about limits ----------------
@pytest.fixture(autouse=True)
def _tests_default_to_no_limits(monkeypatch):
    """Every test gets its own in-memory limiter with limits too high to matter (and no Redis needed). A test about limits sets low
    values and, if it wants the real thing, installs a RedisStore itself."""
    from indexing import ratelimit
    for name in ("rate_login_failures", "rate_login_per_address", "rate_ask_per_address_minute", "rate_ask_per_minute", "rate_ask_per_day", "rate_ask_concurrent", "rate_actions_per_minute", "rate_agent_per_minute", "rate_agent_per_day"):
        monkeypatch.setattr(settings, name, 10 ** 6)
    ratelimit.set_limiter(ratelimit.Limiter(ratelimit.MemoryStore()))
    yield
    ratelimit.set_limiter(None)


# ---------------- logs: tests must never write into the real log file ----------------
@pytest.fixture(autouse=True)
def _logs_go_to_a_temp_file(tmp_path, monkeypatch):
    """Every test logs into its own temp file. Before this, test runs added hundreds of lines
    (including fake error messages) to data/logs/ingestion.jsonl."""
    monkeypatch.setattr(settings, "log_path", tmp_path / "test_logs" / "ingestion.jsonl")
    from ingestion.logs import get_logger
    get_logger()        # structlog only switches files when get_logger() is called: do it now
