-- 002: remember what happened when each document version was indexed.
ALTER TABLE documents ADD COLUMN indexed_at   timestamptz;
ALTER TABLE documents ADD COLUMN chunk_count  int;
ALTER TABLE documents ADD COLUMN embed_model  text;
ALTER TABLE documents ADD COLUMN index_error  text;
