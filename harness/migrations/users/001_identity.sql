CREATE TABLE user_accounts (
    actor_id text PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE user_memberships (
    actor_id text NOT NULL REFERENCES user_accounts(actor_id),
    workspace_id text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    roles text[] NOT NULL CHECK(roles <@ ARRAY['reader','operator','approver']::text[]),
    projects text[] NOT NULL,
    repositories text[] NOT NULL,
    jobs text[] NOT NULL,
    PRIMARY KEY(actor_id,workspace_id)
);
CREATE TABLE user_sessions (
    session_id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE CHECK(token_hash ~ '^[0-9a-f]{64}$'),
    actor_id text NOT NULL,
    workspace_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY(actor_id,workspace_id) REFERENCES user_memberships(actor_id,workspace_id),
    CHECK(expires_at>created_at)
);
CREATE INDEX user_sessions_expiry ON user_sessions(expires_at);
