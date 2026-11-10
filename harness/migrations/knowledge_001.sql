CREATE TABLE knowledge_inventory (
    workspace_id text PRIMARY KEY, revision bigint NOT NULL DEFAULT 0, active_index text
);
CREATE TABLE knowledge_documents (
    document_id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES knowledge_inventory,
    owner text NOT NULL, filename text NOT NULL, content text NOT NULL, content_hash text NOT NULL,
    readers text[] NOT NULL, state text NOT NULL CHECK(state IN ('ACTIVE','RETIRED')),
    UNIQUE(workspace_id, document_id)
);
CREATE TABLE knowledge_indexes (
    index_id text PRIMARY KEY, workspace_id text NOT NULL REFERENCES knowledge_inventory,
    revision bigint NOT NULL, configuration text NOT NULL,
    state text NOT NULL CHECK(state IN ('BUILDING','READY','FAILED','STALE')),
    collection_name text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE knowledge_chunks (
    index_id text NOT NULL REFERENCES knowledge_indexes, chunk_id text NOT NULL,
    document_id text NOT NULL REFERENCES knowledge_documents, ordinal integer NOT NULL,
    content text NOT NULL, content_hash text NOT NULL,
    PRIMARY KEY(index_id, chunk_id)
);
