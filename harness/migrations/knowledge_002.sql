CREATE TABLE knowledge_jobs (
    job_id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES knowledge_inventory,
    requested_by text NOT NULL, request_key text NOT NULL,
    inventory_revision bigint NOT NULL, configuration text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','DONE','FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
    owner text, fencing_token bigint NOT NULL DEFAULT 0, lease_until timestamptz,
    deadline_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '5 minutes',
    index_id text REFERENCES knowledge_indexes, last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(workspace_id,requested_by,request_key)
);
CREATE INDEX knowledge_jobs_claim ON knowledge_jobs(workspace_id,requested_by,created_at)
    WHERE status IN ('PENDING','RUNNING');
