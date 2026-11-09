ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
    CHECK (status IN ('PENDING', 'RUNNING', 'PAUSED', 'DONE', 'FAILED', 'CANCELLED'));
ALTER TABLE jobs
    ADD COLUMN owner text,
    ADD COLUMN fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    ADD COLUMN lease_until timestamptz,
    ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    ADD COLUMN max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts > 0),
    ADD COLUMN pause_at text NOT NULL DEFAULT 'none' CHECK (pause_at IN ('none','jira','github','jenkins','failure')),
    ADD COLUMN last_error text,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD CONSTRAINT jobs_owner_lease CHECK (
        (status = 'RUNNING' AND owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (status <> 'RUNNING' AND owner IS NULL AND lease_until IS NULL));
CREATE INDEX jobs_claimable ON jobs (status, lease_until, created_at);
