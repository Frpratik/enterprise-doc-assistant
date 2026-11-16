-- 006: who asked for what (Phase 5, Stage 4): an append-only audit log.
--
-- It records WHO (user, company), WHEN, WHICH documents were put in front of the model and which chunks the answer cited, and how
-- it ended. It deliberately does NOT record the question text or the answer text (only the question's length): the log must be
-- safe to keep and to show to an auditor. A failed login stores a keyed hash of the username (so repeated attacks on one account
-- can be seen without storing what an attacker typed).
--
-- Append-only: UPDATE, DELETE and TRUNCATE are refused by triggers. (A database administrator can still drop the triggers: this
-- stops mistakes and application bugs, not a hostile administrator. Shipping the rows to a separate system is a Phase 9 topic.)

CREATE TABLE audit_log (
    audit_id        bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    request_id      text NOT NULL,
    event           text NOT NULL,
    user_id         text,
    tenant_id       text,
    subject_hash    text,
    doc_ids         text[] NOT NULL DEFAULT '{}',
    chunk_ids       text[] NOT NULL DEFAULT '{}',
    status          text,
    model           text,
    question_chars  integer,
    latency_ms      integer,
    CONSTRAINT audit_log_event_check CHECK (event IN ('login_ok', 'login_failed', 'ask_retrieved', 'ask_finished'))
);

CREATE INDEX audit_log_user_idx ON audit_log (user_id, at);
CREATE INDEX audit_log_at_idx ON audit_log (at);

CREATE FUNCTION audit_log_refuse_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_refuse_change();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_refuse_change();
