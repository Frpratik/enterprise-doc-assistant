-- 003: keyword search also looks at the heading path (weight A) as well as the text (weight B).
-- Why: a question like "reimbursement limits" should find the chunk under that heading,
-- even if the body text uses other words.
DROP INDEX IF EXISTS chunks_tsv_idx;
ALTER TABLE chunks DROP COLUMN tsv;
ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(heading_path, '')), 'A') ||
    setweight(to_tsvector('english', content), 'B')
) STORED;
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
