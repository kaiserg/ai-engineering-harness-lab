ALTER TABLE jobs ADD COLUMN producer_traceparent varchar(55),
    ADD COLUMN previous_traceparent varchar(55),
    ADD COLUMN telemetry_enqueued_at timestamptz NOT NULL DEFAULT clock_timestamp();
