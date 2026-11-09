CREATE TABLE write_jobs (
    job_id text PRIMARY KEY,
    operation_id text NOT NULL UNIQUE REFERENCES operations(operation_id),
    status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','DONE','NEEDS_REVIEW')),
    interrupt text CHECK(interrupt IN ('before-send','after-send')),
    owner text,
    fencing_token bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
    lease_until timestamptz,
    not_before timestamptz NOT NULL DEFAULT clock_timestamp(),
    result_status text,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((status='RUNNING' AND owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (status<>'RUNNING' AND lease_until IS NULL))
);
CREATE INDEX write_jobs_pending ON write_jobs(status,not_before,created_at);
CREATE FUNCTION protect_write_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.job_id,NEW.operation_id,NEW.interrupt,NEW.created_at) IS DISTINCT FROM
       (OLD.job_id,OLD.operation_id,OLD.interrupt,OLD.created_at)
       OR NEW.fencing_token<OLD.fencing_token OR NEW.attempts<OLD.attempts THEN
        RAISE EXCEPTION 'write job identity or counters immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER write_job_guard BEFORE UPDATE ON write_jobs FOR EACH ROW EXECUTE FUNCTION protect_write_job();
