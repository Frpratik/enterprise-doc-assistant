-- 009: the approval flow (Phase 7, Stage 2).
--
-- The assistant only PROPOSES. A proposal is stored as a pending action; the requester confirms it (a separate request, with the fingerprint of
-- the draft they were shown); a ticket is then created, a leave request is created and waits for a manager. What runs is read from the stored
-- row, never from the confirming request, and each action can run ONCE (the unique action_id on the created rows makes a double run impossible
-- even if the application had a bug).

CREATE TABLE pending_actions (
    action_id      text PRIMARY KEY,
    kind           text NOT NULL,
    user_id        text NOT NULL,
    tenant_id      text NOT NULL,
    args           jsonb NOT NULL,
    fingerprint    text NOT NULL,
    summary        text NOT NULL,
    status         text NOT NULL DEFAULT 'awaiting_requester',
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    confirmed_at   timestamptz,
    result_ref     text,
    failure        text,
    CONSTRAINT pending_actions_kind_check   CHECK (kind IN ('leave_request', 'ticket')),
    CONSTRAINT pending_actions_status_check CHECK (status IN ('awaiting_requester', 'executed', 'cancelled', 'expired', 'failed')),
    CONSTRAINT pending_actions_executed_check CHECK (status <> 'executed' OR (confirmed_at IS NOT NULL AND result_ref IS NOT NULL)),
    CONSTRAINT pending_actions_fingerprint_check CHECK (char_length(fingerprint) = 64)
);
CREATE INDEX pending_actions_user_idx ON pending_actions (user_id, status);

ALTER TABLE leave_requests ADD COLUMN action_id text UNIQUE;
ALTER TABLE tickets        ADD COLUMN action_id text UNIQUE;

-- the audit log learns the new events, and which object they are about. Written in the SAME transaction as the change itself:
-- if the audit row cannot be written, the change does not happen.
ALTER TABLE audit_log ADD COLUMN ref text;
ALTER TABLE audit_log ADD COLUMN result_ref text;
ALTER TABLE audit_log DROP CONSTRAINT audit_log_event_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_event_check CHECK (event IN (
    'login_ok', 'login_failed', 'ask_retrieved', 'ask_finished',
    'action_proposed', 'action_executed', 'action_cancelled', 'action_expired', 'action_failed', 'leave_decided', 'leave_cancelled'));
