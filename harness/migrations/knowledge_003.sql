ALTER TABLE knowledge_documents ADD COLUMN created_at timestamptz NOT NULL DEFAULT clock_timestamp();
CREATE TABLE knowledge_uploads (
    workspace_id text NOT NULL, requested_by text NOT NULL, request_key text NOT NULL,
    document_id text NOT NULL REFERENCES knowledge_documents, payload_hash text NOT NULL,
    PRIMARY KEY(workspace_id,requested_by,request_key)
);
