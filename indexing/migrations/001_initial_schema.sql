-- 001: tables for documents, chunks and embeddings.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per document VERSION. Replaces data/manifest.json from Phase 1.
CREATE TABLE documents (
    doc_id         text        NOT NULL,
    version        int         NOT NULL,
    file_name      text        NOT NULL,
    file_type      text,
    hash           text        NOT NULL,             -- SHA-256 of the file
    status         text        NOT NULL,             -- queued / processing / done / needs_review / failed
    status_reason  text,
    doc_label      text,
    language       text,
    title          text,
    page_count     int,
    quality_score  real,
    source_path    text,
    original_path  text,
    output_path    text,
    is_active      boolean     NOT NULL DEFAULT false,  -- only ONE version of a doc is active (searchable)
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz,                       -- soft delete
    PRIMARY KEY (doc_id, version)
);
CREATE INDEX documents_hash_idx   ON documents (hash);
CREATE INDEX documents_name_idx   ON documents (lower(file_name));
CREATE INDEX documents_active_idx ON documents (is_active);

-- Embeddings are stored ONCE per (text, model): this table is also the embedding cache.
-- 'embedding' has no fixed size so different models can live side by side;
-- each model gets its own HNSW index (created by the indexer) with its own size.
CREATE TABLE embeddings (
    embed_hash   text        NOT NULL,                -- SHA-256 of the exact text that was embedded
    embed_model  text        NOT NULL,
    dims         int         NOT NULL,
    embedding    vector      NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (embed_hash, embed_model)
);

-- One row per chunk of a document version.
CREATE TABLE chunks (
    chunk_id       text        PRIMARY KEY,
    doc_id         text        NOT NULL,
    version        int         NOT NULL,
    chunk_index    int         NOT NULL,
    chunk_type     text        NOT NULL,              -- text / table / slide / sheet
    content        text        NOT NULL,              -- shown to the user / LLM
    embed_text     text        NOT NULL,              -- title + heading path + content (what gets embedded)
    embed_hash     text        NOT NULL,              -- links to embeddings.embed_hash
    token_count    int         NOT NULL,
    heading_path   text,
    location       jsonb       NOT NULL DEFAULT '{}', -- pages / slides / sheet
    metadata       jsonb       NOT NULL DEFAULT '{}',
    tenant_id      text        NOT NULL DEFAULT 'default',   -- used in Phase 5 (access control)
    allowed_roles  text[]      NOT NULL DEFAULT '{all}',     -- used in Phase 5
    is_active      boolean     NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,  -- keyword search (Phase 3)
    UNIQUE (doc_id, version, chunk_index),
    FOREIGN KEY (doc_id, version) REFERENCES documents (doc_id, version) ON DELETE CASCADE
);
CREATE INDEX chunks_doc_active_idx ON chunks (doc_id, is_active);
CREATE INDEX chunks_embed_hash_idx ON chunks (embed_hash);
CREATE INDEX chunks_tsv_idx        ON chunks USING gin (tsv);
