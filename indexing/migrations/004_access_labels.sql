-- 004: who may see a document (Phase 5: access control). FAIL CLOSED.
--
-- One source of truth: the DOCUMENT carries the access label, every one of its chunks inherits it through the join that
-- every search already makes. (The old per-chunk columns tenant_id / allowed_roles were never used by any code: dropped,
-- so two copies of the rules can never disagree.)
--
--   access_status   'unlabelled' (the default)  nobody can see it, whatever their role
--                   'labelled'                  tenant_id + allowed_roles decide who can
--   allowed_roles   the roles that may see it; the word 'all' means every signed-in person of that tenant
--   access_source   'none' | 'rule' (from access_rules.json) | 'manual' (set by a person, survives new versions)
--
-- Existing documents become 'unlabelled' = invisible until someone labels them (run: python -m indexing.manage apply-rules).

ALTER TABLE documents ADD COLUMN tenant_id         text        NOT NULL DEFAULT 'unassigned';
ALTER TABLE documents ADD COLUMN allowed_roles     text[]      NOT NULL DEFAULT '{}';
ALTER TABLE documents ADD COLUMN access_status     text        NOT NULL DEFAULT 'unlabelled';
ALTER TABLE documents ADD COLUMN access_source     text        NOT NULL DEFAULT 'none';
ALTER TABLE documents ADD COLUMN access_updated_at timestamptz;

-- The database itself refuses a half-labelled document (a label with no tenant or no roles would be ambiguous).
ALTER TABLE documents ADD CONSTRAINT documents_access_status_check CHECK (access_status IN ('labelled', 'unlabelled'));
ALTER TABLE documents ADD CONSTRAINT documents_access_source_check CHECK (access_source IN ('none', 'rule', 'manual'));
ALTER TABLE documents ADD CONSTRAINT documents_access_complete_check CHECK (
    (access_status = 'unlabelled' AND allowed_roles = '{}' AND access_source = 'none')
 OR (access_status = 'labelled' AND tenant_id <> 'unassigned' AND cardinality(allowed_roles) > 0 AND access_source <> 'none'));

CREATE INDEX documents_access_idx ON documents (tenant_id, access_status);

ALTER TABLE chunks DROP COLUMN tenant_id;
ALTER TABLE chunks DROP COLUMN allowed_roles;
