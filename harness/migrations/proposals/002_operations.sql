ALTER TABLE proposals DROP CONSTRAINT proposals_status_check;
ALTER TABLE proposals ADD CHECK (status IN ('PENDING_APPROVAL','APPROVED','REJECTED','EXPIRED','CONSUMED','INVALIDATED'));
CREATE OR REPLACE FUNCTION protect_proposal_payload() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.proposal_id,NEW.run_id,NEW.workspace_id,NEW.requested_by,NEW.request_key,
        NEW.request_hash,NEW.canonical_payload,NEW.payload_hash,NEW.expires_at)
        IS DISTINCT FROM
       (OLD.proposal_id,OLD.run_id,OLD.workspace_id,OLD.requested_by,OLD.request_key,
        OLD.request_hash,OLD.canonical_payload,OLD.payload_hash,OLD.expires_at)
       OR NEW.version <> OLD.version + 1
       OR NOT ((OLD.status='PENDING_APPROVAL' AND NEW.status IN ('APPROVED','REJECTED','EXPIRED'))
           OR (OLD.status='APPROVED' AND NEW.status IN ('CONSUMED','INVALIDATED','EXPIRED')))
       OR (OLD.status='APPROVED' AND (NEW.decided_by,NEW.decided_at) IS DISTINCT FROM (OLD.decided_by,OLD.decided_at)) THEN
        RAISE EXCEPTION 'proposal immutable or invalid transition';
    END IF;
    RETURN NEW;
END $$;
CREATE TABLE operations (
    operation_id text PRIMARY KEY,
    proposal_id text NOT NULL UNIQUE REFERENCES proposals(proposal_id),
    status text NOT NULL CHECK(status IN ('PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED','NOT_APPLIED')),
    version bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    receipt text,
    outcome_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE operation_events (
    operation_id text NOT NULL REFERENCES operations(operation_id),
    version bigint NOT NULL,
    status text NOT NULL,
    actor_id text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(operation_id,version)
);
CREATE FUNCTION protect_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.operation_id,NEW.proposal_id,NEW.created_at) IS DISTINCT FROM (OLD.operation_id,OLD.proposal_id,OLD.created_at)
       OR NEW.version <> OLD.version+1
       OR NOT ((OLD.status='PREPARED' AND NEW.status IN ('DISPATCHING','NOT_APPLIED'))
            OR (OLD.status='DISPATCHING' AND NEW.status IN ('UNKNOWN','SUCCEEDED','NOT_APPLIED'))
            OR (OLD.status='UNKNOWN' AND NEW.status='SUCCEEDED')) THEN
        RAISE EXCEPTION 'invalid operation transition';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER operation_guard BEFORE UPDATE ON operations FOR EACH ROW EXECUTE FUNCTION protect_operation();
