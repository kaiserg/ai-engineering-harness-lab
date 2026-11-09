-- Correlación opcional. No concede permiso ni modifica estado/contadores del trabajo.
ALTER TABLE write_jobs ADD COLUMN producer_traceparent varchar(55);
ALTER TABLE write_jobs ADD COLUMN previous_traceparent varchar(55);
