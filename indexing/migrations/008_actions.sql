-- 008: the systems the assistant can act on (Phase 7, Stage 1): leave and IT tickets. A SIMULATED back-end: in a real company these would be
-- the HR system and the ticket system; the assistant's tools talk to them through the domain layer in actions/domain.py.
--
-- Nothing here is written by the model. The model can only PROPOSE (Stage 2: a person approves, then the system writes).

CREATE TABLE leave_balances (
    user_id      text NOT NULL,
    leave_type   text NOT NULL,
    year         integer NOT NULL,
    annual_days  numeric(5,1) NOT NULL CHECK (annual_days >= 0),
    PRIMARY KEY (user_id, leave_type, year),
    CONSTRAINT leave_balances_type_check CHECK (leave_type IN ('sick', 'casual', 'earned'))
);

CREATE TABLE leave_requests (
    request_id     text PRIMARY KEY,
    user_id        text NOT NULL,
    tenant_id      text NOT NULL,
    leave_type     text NOT NULL,
    start_date     date NOT NULL,
    end_date       date NOT NULL,
    days           numeric(4,1) NOT NULL,
    reason         text NOT NULL,
    status         text NOT NULL DEFAULT 'pending_manager',
    created_at     timestamptz NOT NULL DEFAULT now(),
    decided_by     text,
    decided_at     timestamptz,
    decision_note  text,
    CONSTRAINT leave_requests_type_check   CHECK (leave_type IN ('sick', 'casual', 'earned')),
    CONSTRAINT leave_requests_status_check CHECK (status IN ('pending_manager', 'approved', 'rejected', 'cancelled')),
    CONSTRAINT leave_requests_dates_check  CHECK (end_date >= start_date AND days > 0 AND start_date >= date '2000-01-01'),
    CONSTRAINT leave_requests_reason_check CHECK (char_length(reason) BETWEEN 3 AND 500),
    CONSTRAINT leave_requests_decision_check CHECK ((status IN ('approved', 'rejected')) = (decided_by IS NOT NULL))
);
CREATE INDEX leave_requests_user_idx ON leave_requests (user_id, status, start_date);

CREATE SEQUENCE ticket_numbers START 1000;

CREATE TABLE tickets (
    ticket_id    text PRIMARY KEY DEFAULT ('TCK-' || nextval('ticket_numbers')),
    user_id      text NOT NULL,
    tenant_id    text NOT NULL,
    category     text NOT NULL,
    priority     text NOT NULL DEFAULT 'normal',
    title        text NOT NULL,
    description  text NOT NULL,
    status       text NOT NULL DEFAULT 'open',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tickets_category_check CHECK (category IN ('hardware', 'software', 'access', 'network', 'other')),
    CONSTRAINT tickets_priority_check CHECK (priority IN ('low', 'normal', 'high')),
    CONSTRAINT tickets_status_check   CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    CONSTRAINT tickets_title_check    CHECK (char_length(title) BETWEEN 3 AND 120),
    CONSTRAINT tickets_description_check CHECK (char_length(description) BETWEEN 3 AND 2000)
);
CREATE INDEX tickets_user_idx ON tickets (user_id, status);
