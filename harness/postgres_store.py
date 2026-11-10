"""Store PostgreSQL, productor transaccional y reclamos con lease/fencing."""
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass
import math
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import ContractError, RunState
from .storage import check_new, check_owner, check_update, decode, encode


@dataclass(frozen=True)
class Claim:
    job_id: str
    run_id: str
    owner: str
    fencing_token: int
    workspace_id: str
    requested_by: str
    pause_at: str
    attempts: int
    max_attempts: int
    producer_traceparent: str | None = None
    previous_traceparent: str | None = None
    telemetry_enqueued_at: object = None


class PostgresRunStore:
    def __init__(self, dsn, *, schema='harness_lab'):
        self._dsn, self.schema = dsn, schema
        self._claim = None
        self._migrate()

    @contextmanager
    def _database(self):
        try:
            with psycopg.connect(self._dsn, connect_timeout=5, row_factory=dict_row,
                                options='-c statement_timeout=5000 -c lock_timeout=3000') as db:
                db.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.schema)))
                yield db  # El contexto confirma al salir o revierte ante cualquier excepción.
        except psycopg.OperationalError:
            raise ContractError('DATABASE_UNAVAILABLE') from None
        except psycopg.Error:
            raise ContractError('DATABASE_ERROR') from None

    def _migrate(self):
        with self._database() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('harness-migration:' + self.schema,))
            db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            db.execute('CREATE TABLE IF NOT EXISTS schema_version (singleton boolean PRIMARY KEY CHECK (singleton), version integer NOT NULL)')
            row = db.execute('SELECT version FROM schema_version WHERE singleton = true').fetchone()
            if row and row['version'] not in (1, 2, 3):
                raise ContractError('STORAGE_SCHEMA_UNSUPPORTED')
            if row is None:
                db.execute((Path(__file__).parent / 'migrations/001_postgres.sql').read_text())
                db.execute('INSERT INTO schema_version VALUES (true, 1)')
            if row is None or row['version'] == 1:
                db.execute((Path(__file__).parent / 'migrations/002_worker.sql').read_text())
                db.execute('UPDATE schema_version SET version = 2')
            if row is None or row['version'] in (1, 2):
                db.execute((Path(__file__).parent / 'migrations/003_traces.sql').read_text())
                db.execute('UPDATE schema_version SET version = 3')

    @staticmethod
    def _restore(row, identity):
        run = decode(json.dumps(row['payload']))
        if (run.run_id != row['run_id'] or run.version != row['version']
                or run.workspace_id != row['workspace_id'] or run.requested_by != row['requested_by']):
            raise ContractError('CORRUPT_STATE')
        check_owner(run, identity)
        return run

    def _load(self, db, run_id, identity, *, locked=False):
        query = '''SELECT * FROM runs WHERE run_id = %s AND workspace_id = %s AND requested_by = %s'''
        row = db.execute(query + (' FOR UPDATE' if locked else ''),
                         (run_id, identity.workspace_id, identity.actor_id)).fetchone()
        if row is None:
            raise ContractError('NOT_FOUND')
        return self._restore(row, identity)

    @staticmethod
    def _insert(db, run):
        db.execute('INSERT INTO runs VALUES (%s, %s, %s, %s, %s, %s)',
                   (run.run_id, run.workspace_id, run.requested_by, run.version, run.created_at,
                    Jsonb(json.loads(encode(run)))))

    def create(self, run, identity):
        """Contrato RunStore para ejecución directa: no crea un trabajo que nadie reclamó."""
        return self._create(run, identity)

    def enqueue(self, run, identity, *, job_id=None, pause_at='none', max_pending=None):
        """Productor durable: crear run y trabajo como una sola unidad de commit."""
        if pause_at not in ('none', 'jira', 'github', 'jenkins', 'failure'):
            raise ContractError('INVALID_PAUSE')
        return self._create(run, identity, job_id=job_id or str(uuid4()), pause_at=pause_at, max_pending=max_pending)

    def _create(self, run, identity, *, job_id=None, pause_at='none', max_pending=None):
        check_new(run, identity)
        with self._database() as db:
            self._admit(db,run,identity,job_id=job_id,pause_at=pause_at,max_pending=max_pending)
        return decode(encode(run))

    def _admit(self,db,run,identity,*,job_id=None,pause_at="none",max_pending=None):
        check_new(run,identity)
        if max_pending is not None:
            self._limit(max_pending)
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                       (f'admission:{self.schema}:{identity.workspace_id}:{identity.actor_id}',))
            count = db.execute("SELECT count(*) AS n FROM jobs WHERE workspace_id = %s AND requested_by = %s AND status IN ('PENDING','RUNNING')",
                               (identity.workspace_id, identity.actor_id)).fetchone()['n']
            if count >= max_pending:
                raise ContractError('QUEUE_FULL')
        try:
            self._insert(db, run)
            if job_id is not None:
                from .telemetry import carrier
                db.execute('INSERT INTO jobs (job_id, run_id, workspace_id, requested_by, pause_at, producer_traceparent) VALUES (%s, %s, %s, %s, %s, %s)',
                           (job_id, run.run_id, run.workspace_id, run.requested_by, pause_at, carrier()))
        except psycopg.errors.UniqueViolation:
            raise ContractError('CONFLICT') from None

    def load(self, run_id, identity):
        with self._database() as db:
            return self._load(db, run_id, identity)

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError('INVALID_ARGUMENT')

    def operational_snapshot(self, identity):
        with self._database() as db:
            rows=db.execute("""SELECT 'runs' AS kind, payload->>'status' AS status, count(*) AS n
                FROM runs WHERE workspace_id=%s AND requested_by=%s GROUP BY payload->>'status'
                UNION ALL SELECT 'jobs',status,count(*) FROM jobs
                WHERE workspace_id=%s AND requested_by=%s GROUP BY status
                UNION ALL SELECT 'expired','RUNNING',count(*) FROM jobs
                WHERE workspace_id=%s AND requested_by=%s AND status='RUNNING' AND lease_until<=clock_timestamp()""",
                (identity.workspace_id,identity.actor_id)*3).fetchall()
        snapshot={'runs':{},'jobs':{},'expired_leases':0}
        for row in rows:
            if row['kind']=='expired':
                snapshot['expired_leases']=row['n']
            else:
                snapshot[row['kind']][row['status']]=row['n']
        return snapshot

    def list(self, identity, limit=30):
        self._limit(limit)
        with self._database() as db:
            rows = db.execute('''SELECT * FROM runs WHERE workspace_id = %s AND requested_by = %s
                ORDER BY created_at DESC, run_id LIMIT %s''',
                (identity.workspace_id, identity.actor_id, limit)).fetchall()
            return [self._restore(row, identity) for row in rows]

    def pending_jobs(self, identity, limit=30):
        self._limit(limit)
        with self._database() as db:
            return db.execute('''SELECT job_id, run_id, status, created_at FROM jobs
                WHERE workspace_id = %s AND requested_by = %s AND status = 'PENDING'
                ORDER BY created_at, job_id LIMIT %s''',
                (identity.workspace_id, identity.actor_id, limit)).fetchall()

    def save(self, run, identity):
        return self._save(run, identity, recovering=False)

    def requeue_read(self, run, identity):
        return self._save(run, identity, recovering=True)

    def _save(self, run, identity, recovering):
        if self._claim is not None and self._claim.run_id != run.run_id:
            raise ContractError('LEASE_LOST')
        with self._database() as db:
            current = self._load(db, run.run_id, identity, locked=True)
            check_update(current, run, identity, recovering)
            job = db.execute('SELECT * FROM jobs WHERE run_id = %s FOR UPDATE', (run.run_id,)).fetchone()
            if self._claim is not None and job is None:
                raise ContractError('LEASE_LOST')
            if job:
                if self._claim is None:
                    raise ContractError('WORK_NOT_CLAIMED')
                self._guard(db, job, self._claim, identity)
            saved = decode(encode(run))
            saved.version += 1
            query = """UPDATE runs SET version = %s, payload = %s
                WHERE run_id = %s AND workspace_id = %s AND requested_by = %s AND version = %s"""
            params = [saved.version, Jsonb(json.loads(encode(saved))), saved.run_id,
                      identity.workspace_id, identity.actor_id, current.version]
            if job:
                query += " AND EXISTS (SELECT 1 FROM jobs WHERE job_id = %s AND status = 'RUNNING' AND owner = %s AND fencing_token = %s AND lease_until > clock_timestamp())"
                params += [self._claim.job_id, self._claim.owner, self._claim.fencing_token]
            if db.execute(query, params).rowcount != 1:
                raise ContractError('LEASE_LOST' if job else 'CONFLICT')
            if job and saved.status in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
                self._finish(db, job['job_id'], saved)

        return saved

    def bind(self, claim):
        bound = copy(self)
        bound._claim = claim
        return bound

    @staticmethod
    def _duration(seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0.1 <= seconds <= 3600:
            raise ContractError('INVALID_ARGUMENT')

    def claim_next(self, owner, identity, *, lease_seconds=15):
        self._duration(lease_seconds)
        if type(owner) is not str or not owner or len(owner) > 100:
            raise ContractError('INVALID_ARGUMENT')
        with self._database() as db:
            row = db.execute('''WITH candidate AS (
                SELECT job_id FROM jobs WHERE workspace_id = %s AND requested_by = %s
                AND (status = 'PENDING' OR (status = 'RUNNING' AND lease_until <= clock_timestamp()))
                ORDER BY created_at, job_id FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE jobs j SET status = 'RUNNING', owner = %s, fencing_token = fencing_token + 1,
                    attempts = attempts + 1, lease_until = clock_timestamp() + %s * interval '1 second',
                    updated_at = clock_timestamp(), last_error = NULL
                FROM candidate c WHERE j.job_id = c.job_id RETURNING j.*''',
                (identity.workspace_id, identity.actor_id, owner, lease_seconds)).fetchone()
            return Claim(**{field: row[field] for field in Claim.__dataclass_fields__}) if row else None

    @staticmethod
    def _guard(db, job, claim, identity):
        if (job is None or job['job_id'] != claim.job_id or job['run_id'] != claim.run_id
                or job['workspace_id'] != identity.workspace_id or job['requested_by'] != identity.actor_id
                or job['workspace_id'] != claim.workspace_id or job['requested_by'] != claim.requested_by
                or job['status'] != 'RUNNING' or job['owner'] != claim.owner
                or job['fencing_token'] != claim.fencing_token):
            raise ContractError('LEASE_LOST')
        # Consultar el reloj del servidor después de obtener el lock, no el reloj del worker.
        if not db.execute('SELECT %s::timestamptz > clock_timestamp() AS alive', (job['lease_until'],)).fetchone()['alive']:
            raise ContractError('LEASE_LOST')

    def record_attempt_trace(self, claim, identity):
        from .telemetry import carrier
        value=carrier()
        if value is None:
            return
        with self._database() as db:
            job=db.execute('SELECT * FROM jobs WHERE job_id = %s FOR UPDATE',(claim.job_id,)).fetchone()
            self._guard(db,job,claim,identity)
            db.execute('UPDATE jobs SET previous_traceparent = %s WHERE job_id = %s',(value,claim.job_id))

    def assert_claim(self, claim, identity):
        with self._database() as db:
            job = db.execute('SELECT * FROM jobs WHERE job_id = %s FOR UPDATE', (claim.job_id,)).fetchone()
            self._guard(db, job, claim, identity)

    def renew(self, claim, identity, *, lease_seconds=15):
        self._duration(lease_seconds)
        with self._database() as db:
            job = db.execute('SELECT * FROM jobs WHERE job_id = %s FOR UPDATE', (claim.job_id,)).fetchone()
            if (job and job['run_id'] == claim.run_id and job['workspace_id'] == identity.workspace_id
                    and job['requested_by'] == identity.actor_id and job['fencing_token'] == claim.fencing_token
                    and job['status'] in ('DONE', 'FAILED', 'CANCELLED', 'PAUSED')):
                return False  # El propio intento ya confirmó su finalización.
            self._guard(db, job, claim, identity)
            count = db.execute("""UPDATE jobs SET lease_until = clock_timestamp() + %s * interval '1 second',
                updated_at = clock_timestamp() WHERE job_id = %s AND lease_until > clock_timestamp()""",
                (lease_seconds, claim.job_id)).rowcount
            if count != 1:
                raise ContractError('LEASE_LOST')
            return True

    @staticmethod
    def _finish(db, job_id, run):
        status = 'DONE' if run.status == RunState.COMPLETED else str(run.status)
        db.execute('''UPDATE jobs SET status = %s, owner = NULL, lease_until = NULL,
            last_error = %s, updated_at = clock_timestamp() WHERE job_id = %s''',
            (status, run.outcome_code, job_id))

    def finish_terminal(self, claim, identity):
        with self._database() as db:
            run = self._load(db, claim.run_id, identity, locked=True)
            job = db.execute('SELECT * FROM jobs WHERE job_id = %s FOR UPDATE', (claim.job_id,)).fetchone()
            if run.status not in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
                raise ContractError('INVALID_TRANSITION')
            if job and job['run_id'] == run.run_id and job['fencing_token'] == claim.fencing_token and job['status'] in ('DONE', 'FAILED', 'CANCELLED'):
                return
            self._guard(db, job, claim, identity)
            self._finish(db, claim.job_id, run)

    def pause_claim(self, claim, identity):
        with self._database() as db:
            run = self._load(db, claim.run_id, identity, locked=True)
            job = db.execute('SELECT * FROM jobs WHERE job_id = %s FOR UPDATE', (claim.job_id,)).fetchone()
            self._guard(db, job, claim, identity)
            if run.checkpoint != 'READY' or run.pending_call is not None or run.pending_time_seconds != 0:
                raise ContractError('INVALID_CHECKPOINT')
            db.execute("""UPDATE jobs SET status = 'PAUSED', owner = NULL, lease_until = NULL,
                pause_at = 'none', updated_at = clock_timestamp() WHERE job_id = %s AND lease_until > clock_timestamp()""", (claim.job_id,))
            if db.execute('SELECT status FROM jobs WHERE job_id = %s', (claim.job_id,)).fetchone()['status'] != 'PAUSED':
                raise ContractError('LEASE_LOST')

    def requeue_job(self, run_id, identity):
        with self._database() as db:
            run = self._load(db, run_id, identity, locked=True)
            job = db.execute('SELECT * FROM jobs WHERE run_id = %s FOR UPDATE', (run_id,)).fetchone()
            if job is None or run.status not in (RunState.QUEUED, RunState.RUNNING):
                raise ContractError('INVALID_TRANSITION')
            alive = db.execute('SELECT %s::timestamptz > clock_timestamp() AS alive', (job['lease_until'],)).fetchone()['alive']
            if job['status'] != 'PAUSED' and not (job['status'] == 'RUNNING' and not alive):
                raise ContractError('RUN_BUSY')
            from .telemetry import carrier
            db.execute("""UPDATE jobs SET status = 'PENDING', owner = NULL, lease_until = NULL,
                pause_at = 'none', last_error = NULL, updated_at = clock_timestamp(),
                producer_traceparent = %s, telemetry_enqueued_at = clock_timestamp() WHERE job_id = %s""", (carrier(),job['job_id']))

    def snapshot(self, run_id, identity):
        # Un solo snapshot SQL: evitar combinar un run viejo con un job recién finalizado.
        with self._database() as db:
            row = db.execute("""SELECT r.*, to_jsonb(j) AS job, j.lease_until > clock_timestamp() AS lease_active
                FROM runs r LEFT JOIN jobs j ON j.run_id = r.run_id
                WHERE r.run_id = %s AND r.workspace_id = %s AND r.requested_by = %s""",
                (run_id, identity.workspace_id, identity.actor_id)).fetchone()
            if row is None:
                raise ContractError('NOT_FOUND')
            job = row['job']
            if job is not None:
                job['lease_active'] = row['lease_active']
                for field in ('producer_traceparent','previous_traceparent','telemetry_enqueued_at'):
                    job.pop(field,None)
            return self._restore(row, identity), job

    def job_state(self, run_id, identity):
        with self._database() as db:
            return db.execute('''SELECT job_id, run_id, status, owner, fencing_token, attempts, max_attempts,
                lease_until, pause_at, last_error, lease_until > clock_timestamp() AS lease_active
                FROM jobs WHERE run_id = %s AND workspace_id = %s AND requested_by = %s''',
                (run_id, identity.workspace_id, identity.actor_id)).fetchone()
