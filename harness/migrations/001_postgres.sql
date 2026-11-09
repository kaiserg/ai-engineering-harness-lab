CREATE TABLE runs (
    run_id text PRIMARY KEY,
    workspace_id text NOT NULL,
    requested_by text NOT NULL,
    version bigint NOT NULL CHECK (version >= 0),
    created_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE (run_id, workspace_id, requested_by)
);
CREATE INDEX runs_owner_created ON runs (workspace_id, requested_by, created_at DESC);

-- Solo el productor está implementado. Claim/lease y estados de ejecución llegan después.
CREATE TABLE jobs (
    job_id text PRIMARY KEY,
    run_id text NOT NULL UNIQUE,
    workspace_id text NOT NULL,
    requested_by text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status = 'PENDING'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, workspace_id, requested_by)
        REFERENCES runs (run_id, workspace_id, requested_by)
);
CREATE INDEX jobs_owner_pending ON jobs (workspace_id, requested_by, created_at);
