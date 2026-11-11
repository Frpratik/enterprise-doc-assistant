"""Phase 2 Stage 0 tests: database connection, migrations, pgvector."""
import numpy as np
import psycopg
import pytest

from indexing.db import connect
from indexing.migrate import applied, migrate


def test_migrations_create_all_tables(db_url):
    with connect() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"documents", "chunks", "embeddings", "schema_migrations"} <= tables
    assert "001_initial_schema.sql" in applied(db_url)


def test_migrations_run_only_once(db_url):
    assert migrate(db_url, quiet=True) == []          # already applied -> nothing new


def test_vectors_round_trip(db_url):
    vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    with connect() as conn:
        conn.execute("INSERT INTO embeddings (embed_hash, embed_model, dims, embedding) VALUES (%s, %s, %s, %s)",
                     ("h1", "test-model", 3, vec))
        back = conn.execute("SELECT embedding FROM embeddings WHERE embed_hash = 'h1'").fetchone()[0]
    assert np.allclose(back.to_numpy(), vec)          # pgvector returns a Vector object


def test_failed_block_rolls_back(db_url):
    with pytest.raises(RuntimeError):
        with connect() as conn:
            conn.execute("INSERT INTO documents (doc_id, version, file_name, hash, status) "
                         "VALUES ('d1', 1, 'a.pdf', 'h', 'done')")
            raise RuntimeError("crash in the middle")
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0   # nothing half-saved


def test_chunk_needs_its_document(db_url):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with connect() as conn:
            conn.execute("INSERT INTO chunks (chunk_id, doc_id, version, chunk_index, chunk_type, content, "
                         "embed_text, embed_hash, token_count) VALUES ('c1','missing',1,0,'text','x','x','h',1)")


def test_keyword_column_is_filled_automatically(db_url):
    with connect() as conn:
        conn.execute("INSERT INTO documents (doc_id, version, file_name, hash, status) VALUES ('d1',1,'a','h','done')")
        conn.execute("INSERT INTO chunks (chunk_id, doc_id, version, chunk_index, chunk_type, content, embed_text, "
                     "embed_hash, token_count) VALUES ('c1','d1',1,0,'text','Employees get 12 sick leaves',"
                     "'x','h',5)")
        hit = conn.execute("SELECT count(*) FROM chunks WHERE tsv @@ plainto_tsquery('english', 'sick leave')"
                           ).fetchone()[0]
    assert hit == 1                                   # 'leaves' matched 'leave' (stemming)
