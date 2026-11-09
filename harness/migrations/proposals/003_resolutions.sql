CREATE TABLE operation_resolutions (
    operation_id text PRIMARY KEY REFERENCES operations(operation_id),
    operation_version bigint NOT NULL,
    canonical_payload text NOT NULL,
    payload_hash text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING_REVIEW' CHECK(status IN ('PENDING_REVIEW','CONFIRMED')),
    version bigint NOT NULL DEFAULT 0,
    requested_by text NOT NULL,
    reviewed_by text,
    reviewed_at timestamptz,
    CHECK ((status='PENDING_REVIEW' AND reviewed_by IS NULL AND reviewed_at IS NULL AND version=0)
        OR (status='CONFIRMED' AND reviewed_by IS NOT NULL AND reviewed_by<>requested_by AND reviewed_at IS NOT NULL AND version=1))
);
CREATE FUNCTION protect_resolution() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.operation_id,NEW.operation_version,NEW.canonical_payload,NEW.payload_hash,NEW.requested_by)
       IS DISTINCT FROM (OLD.operation_id,OLD.operation_version,OLD.canonical_payload,OLD.payload_hash,OLD.requested_by)
       OR OLD.status<>'PENDING_REVIEW' OR NEW.status<>'CONFIRMED' OR NEW.version<>OLD.version+1 THEN
        RAISE EXCEPTION 'resolution immutable or invalid transition';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER resolution_guard BEFORE UPDATE ON operation_resolutions FOR EACH ROW EXECUTE FUNCTION protect_resolution();
CREATE OR REPLACE FUNCTION protect_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.operation_id,NEW.proposal_id,NEW.created_at) IS DISTINCT FROM (OLD.operation_id,OLD.proposal_id,OLD.created_at)
       OR NEW.version <> OLD.version+1
       OR NOT ((OLD.status='PREPARED' AND NEW.status IN ('DISPATCHING','NOT_APPLIED'))
            OR (OLD.status='DISPATCHING' AND NEW.status IN ('UNKNOWN','SUCCEEDED','NOT_APPLIED'))
            OR (OLD.status='UNKNOWN' AND NEW.status='SUCCEEDED')
            OR (OLD.status='UNKNOWN' AND NEW.status='NOT_APPLIED' AND EXISTS (
                SELECT 1 FROM operation_resolutions r WHERE r.operation_id=OLD.operation_id
                    AND r.operation_version=OLD.version AND r.status='CONFIRMED'))) THEN
        RAISE EXCEPTION 'invalid operation transition';
    END IF;
    RETURN NEW;
END $$;
