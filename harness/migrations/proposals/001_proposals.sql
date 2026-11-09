CREATE TABLE proposals (
    proposal_id text PRIMARY KEY,
    run_id text NOT NULL,
    workspace_id text NOT NULL,
    requested_by text NOT NULL,
    request_key text NOT NULL,
    request_hash text NOT NULL,
    canonical_payload text NOT NULL,
    payload_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'PENDING_APPROVAL'
        CHECK (status IN ('PENDING_APPROVAL','APPROVED','REJECTED','EXPIRED')),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    decided_by text,
    decided_at timestamptz,
    UNIQUE (workspace_id, requested_by, request_key),
    FOREIGN KEY (run_id, workspace_id, requested_by) REFERENCES runs (run_id, workspace_id, requested_by),
    CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
    CHECK (status NOT IN ('APPROVED','REJECTED') OR (decided_by IS NOT NULL AND decided_by <> requested_by))
);
CREATE FUNCTION protect_proposal_payload() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.proposal_id,NEW.run_id,NEW.workspace_id,NEW.requested_by,NEW.request_key,
        NEW.request_hash,NEW.canonical_payload,NEW.payload_hash,NEW.expires_at)
        IS DISTINCT FROM
       (OLD.proposal_id,OLD.run_id,OLD.workspace_id,OLD.requested_by,OLD.request_key,
        OLD.request_hash,OLD.canonical_payload,OLD.payload_hash,OLD.expires_at)
       OR OLD.status <> 'PENDING_APPROVAL'
       OR NEW.status NOT IN ('APPROVED','REJECTED','EXPIRED')
       OR NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'proposal immutable or invalid transition';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER proposal_immutable BEFORE UPDATE ON proposals
    FOR EACH ROW EXECUTE FUNCTION protect_proposal_payload();
