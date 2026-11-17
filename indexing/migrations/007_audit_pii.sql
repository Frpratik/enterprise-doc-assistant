-- 007: the audit log notes WHEN a person pasted personal data into a question, and which kinds (never the values).
-- (Phase 6, Stage 2.) The columns of an append-only table can still be added: the triggers only refuse changes to ROWS.
ALTER TABLE audit_log ADD COLUMN pii_kinds text[] NOT NULL DEFAULT '{}';
