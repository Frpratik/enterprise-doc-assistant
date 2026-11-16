-- 005: people who can sign in (Phase 5, Stage 3).
--
--   password_hash   scrypt hash with its own random salt and parameters: the password itself is never stored
--   roles           the roles the person holds; may be empty (they then see only documents labelled 'all'). Never contains 'all'
--   token_version   bumped when the password changes or the person is disabled: every token issued before that stops working at once
--   is_active       false = cannot sign in and existing tokens stop working on the next request
--
-- A person's tenant and roles are read from this table on EVERY request, not trusted from the token: changing a role here takes effect immediately.

CREATE TABLE users (
    user_id         text PRIMARY KEY,
    username        text NOT NULL UNIQUE,
    tenant_id       text NOT NULL,
    roles           text[] NOT NULL DEFAULT '{}',
    password_hash   text NOT NULL,
    is_active       boolean NOT NULL DEFAULT true,
    token_version   integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_login_at   timestamptz,
    CONSTRAINT users_username_check CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'),
    CONSTRAINT users_tenant_check   CHECK (tenant_id ~ '^[a-z][a-z0-9_-]{1,31}$' AND tenant_id <> 'unassigned'),
    CONSTRAINT users_roles_check    CHECK (NOT ('all' = ANY(roles)))
);

CREATE INDEX users_tenant_idx ON users (tenant_id);
