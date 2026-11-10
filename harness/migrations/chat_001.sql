CREATE TABLE IF NOT EXISTS conversations (
    conversation_id uuid PRIMARY KEY,
    workspace_id text NOT NULL,
    requested_by text NOT NULL,
    request_key uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(workspace_id, requested_by, request_key)
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    conversation_id uuid NOT NULL REFERENCES conversations,
    ordinal integer NOT NULL CHECK(ordinal BETWEEN 1 AND 20),
    request_key uuid NOT NULL,
    pause_after_search boolean NOT NULL DEFAULT false,
    run_id text NOT NULL UNIQUE REFERENCES runs,
    PRIMARY KEY(conversation_id,ordinal),
    UNIQUE(conversation_id,request_key)
);
ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS pause_after_search boolean NOT NULL DEFAULT false;

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS execution_profile text NOT NULL DEFAULT 'ollama-qwen3-4b-document-chat-v1';
