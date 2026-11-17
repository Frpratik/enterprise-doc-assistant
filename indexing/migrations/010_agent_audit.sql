-- 010: the agent leaves a trace (Phase 7, Stage 3): one row when a run STARTS (written before the model sees anything of the person's data;
-- if it cannot be written the run does not happen) and one when it ends, with the names of the tools it called (never their arguments or results).
ALTER TABLE audit_log ADD COLUMN tools text[] NOT NULL DEFAULT '{}';
ALTER TABLE audit_log DROP CONSTRAINT audit_log_event_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_event_check CHECK (event IN (
    'login_ok', 'login_failed', 'ask_retrieved', 'ask_finished',
    'action_proposed', 'action_executed', 'action_cancelled', 'action_expired', 'action_failed', 'leave_decided', 'leave_cancelled',
    'agent_started', 'agent_finished'));
