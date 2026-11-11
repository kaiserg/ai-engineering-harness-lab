"""Store SQLite y reglas compartidas con el adaptador en memoria."""
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import sqlite3

from .contracts import ContractError, Identity, Run, RunState, RUN_TRANSITIONS, ToolCall, ToolResult, READ_TOOLS, ToolFailure, OPTIONAL_READ_ERRORS, tool_arguments


from .profiles import CI_CHAT_PROFILES

def check_owner(run, identity):
    if run.workspace_id != identity.workspace_id or run.requested_by != identity.actor_id:
        raise ContractError('NOT_FOUND')


def check_timing(run):
    for value in (run.active_seconds, run.pending_time_seconds, run.recovered_time_seconds):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ContractError('INVALID_CHECKPOINT')
    if run.recovered_time_seconds > run.active_seconds:
        raise ContractError('INVALID_CHECKPOINT')


def check_failures(run):
    seen = {result.call_id for result in run.results}
    for failure in run.tool_failures:
        if (not isinstance(failure, ToolFailure) or not isinstance(failure.call, ToolCall)
                or failure.code not in OPTIONAL_READ_ERRORS.get(failure.call.name, ())
                or failure.call.call_id in seen):
            raise ContractError('INVALID_CHECKPOINT')
        tool_arguments(failure.call.name, failure.call.arguments)
        seen.add(failure.call.call_id)


def check_read_request(run):
    if run.execution_profile=='ollama-qwen3-4b-document-reader-v1':
        data=run.read_request
        if (type(data) is not dict or set(data)!={'query'} or type(data['query']) is not str
                or not data['query'].strip() or len(data['query'])>2000):raise ContractError('INVALID_CHECKPOINT')
    elif run.execution_profile in CI_CHAT_PROFILES or run.execution_profile in ('ollama-qwen3-4b-document-chat-v1','ollama-qwen3-4b-jira-demo-chat-v1','ollama-qwen3-4b-github-demo-chat-v1','ollama-qwen3-4b-github-public-chat-v1') or run.execution_profile.startswith('ollama-qwen3-4b-jira-cloud-chat-v1:'):
        data=run.read_request
        from uuid import UUID
        try:
            if (type(data) is not dict or set(data)!={'query','conversation_id','history_run_ids'}
                    or type(data['query']) is not str or not data['query'].strip() or len(data['query'])>2000
                    or type(data['history_run_ids']) is not list or len(data['history_run_ids'])>3
                    or len(set(data['history_run_ids']))!=len(data['history_run_ids'])):raise ValueError()
            for identifier in [data['conversation_id'],*data['history_run_ids']]:
                if type(identifier) is not str or str(UUID(identifier))!=identifier:raise ValueError()
        except (ValueError,TypeError,KeyError):raise ContractError('INVALID_CHECKPOINT') from None
    elif run.read_request is not None:raise ContractError('INVALID_CHECKPOINT')


def check_new(run, identity):
    check_read_request(run)
    check_owner(run, identity)
    check_timing(run)
    if run.tool_failures:
        raise ContractError('INVALID_CHECKPOINT')
    if run.timing_version != 1 or run.active_seconds != 0 or run.pending_time_seconds != 0:
        raise ContractError('INVALID_CHECKPOINT')
    if run.status != RunState.QUEUED or run.version != 0:
        raise ContractError('INVALID_TRANSITION')


def check_update(current, run, identity, recovering=False):
    check_owner(current, identity)
    check_owner(run, identity)
    check_timing(run)
    check_failures(run)
    if run.version != current.version:
        raise ContractError('CONFLICT')
    if (run.created_at != current.created_at or run.schema_version != current.schema_version
            or run.active_seconds < current.active_seconds
            or run.recovered_time_seconds < current.recovered_time_seconds
            or run.timing_version != current.timing_version
            or run.execution_profile != current.execution_profile
            or run.read_request != current.read_request
            or (current.assessment is not None and run.assessment != current.assessment)
            or run.model_calls < current.model_calls or run.tool_calls < current.tool_calls
            or (current.limits_snapshot and run.limits_snapshot != current.limits_snapshot)
            or run.results[:len(current.results)] != current.results
            or run.tool_failures[:len(current.tool_failures)] != current.tool_failures):
        raise ContractError('INVALID_CHECKPOINT')
    added = run.tool_failures[len(current.tool_failures):]
    if added and (len(added) != 1 or added[0].call != current.pending_call
                  or run.pending_call is not None or run.checkpoint != 'READY'
                  or run.status != RunState.RUNNING or run.results != current.results
                  or run.pending_time_seconds != 0):
        raise ContractError('INVALID_CHECKPOINT')
    if recovering:
        if asdict(run) != asdict(replace(current, status=RunState.QUEUED)):
            raise ContractError('INVALID_CHECKPOINT')
        if (run.checkpoint == 'TOOL_PENDING') != (run.pending_call is not None):
            raise ContractError('INVALID_CHECKPOINT')
        if current.status != RunState.RUNNING or run.status != RunState.QUEUED:
            raise ContractError('INVALID_TRANSITION')
        if run.pending_call and run.pending_call.name not in READ_TOOLS:
            raise ContractError('UNSAFE_RECOVERY')
        if run.checkpoint not in ('READY', 'MODEL_PENDING', 'TOOL_PENDING'):
            raise ContractError('INVALID_CHECKPOINT')
    elif (current.status not in (RunState.QUEUED, RunState.RUNNING)
          or run.status != current.status and run.status not in RUN_TRANSITIONS.get(current.status, set())):
        raise ContractError('INVALID_TRANSITION')


def encode(run):
    return json.dumps(asdict(run), ensure_ascii=False, allow_nan=False)


def decode(payload):
    try:
        data = json.loads(payload)
        if data['schema_version'] not in (1, 2):
            raise ContractError('STORAGE_SCHEMA_UNSUPPORTED')
        if data['schema_version'] == 1:
            # Los runs previos no midieron tiempo: cero no equivale a consumo conocido.
            data.setdefault('timing_version', 0)
            data.setdefault('active_seconds', 0.0)
            data.setdefault('pending_time_seconds', 0.0)
            data.setdefault('recovered_time_seconds', 0.0)
        if data['schema_version'] == 2 and (not {'active_seconds', 'pending_time_seconds', 'recovered_time_seconds', 'timing_version'} <= data.keys()
                                             or data['timing_version'] != 1):
            raise ContractError('CORRUPT_STATE')
        # Antes de M2 solo existía el backend falso: ese es el default conocido.
        data.setdefault('execution_profile', 'fake-jira-v1')
        data['status'] = RunState(data['status'])
        data['results'] = [ToolResult(**item) for item in data['results']]
        data['tool_failures'] = [ToolFailure(ToolCall(**item['call']), item['code'])
                                 for item in data.get('tool_failures', [])]
        if data['pending_call'] is not None:
            data['pending_call'] = ToolCall(**data['pending_call'])
        run = Run(**data)
        check_read_request(run)
        check_timing(run)
        check_failures(run)
        return run
    except (KeyError, TypeError, ValueError):
        raise ContractError('CORRUPT_STATE') from None


class SQLiteRunStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._database() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ContractError('STORAGE_SCHEMA_UNSUPPORTED')
            db.execute('''CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                requested_by TEXT NOT NULL, version INTEGER NOT NULL,
                payload TEXT NOT NULL)''')
            db.execute('PRAGMA user_version = 1')

    @contextmanager
    def _database(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _load(db, run_id, identity):
        row = db.execute('''SELECT payload FROM runs
            WHERE run_id = ? AND workspace_id = ? AND requested_by = ?''',
                         (run_id, identity.workspace_id, identity.actor_id)).fetchone()
        if row is None:
            raise ContractError('NOT_FOUND')
        run = decode(row[0])
        check_owner(run, identity)
        return run

    def create(self, run: Run, identity: Identity) -> Run:
        check_new(run, identity)
        with self._database() as db:
            try:
                db.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?)',
                           (run.run_id, run.workspace_id, run.requested_by, run.version, encode(run)))
            except sqlite3.IntegrityError:
                raise ContractError('CONFLICT') from None
        return decode(encode(run))

    def load(self, run_id: str, identity: Identity) -> Run:
        with self._database() as db:
            return self._load(db, run_id, identity)

    def list(self, identity: Identity, limit=30):
        with self._database() as db:
            rows = db.execute("""SELECT payload FROM runs WHERE workspace_id = ? AND requested_by = ?
                ORDER BY json_extract(payload, '$.created_at') DESC LIMIT ?""",
                (identity.workspace_id, identity.actor_id, limit)).fetchall()
            runs = [decode(row[0]) for row in rows]
            for run in runs:
                check_owner(run, identity)
            return runs

    def save(self, run: Run, identity: Identity) -> Run:
        return self._save(run, identity, recovering=False)

    def requeue_read(self, run: Run, identity: Identity) -> Run:
        return self._save(run, identity, recovering=True)

    def _save(self, run, identity, recovering):
        # Leer, comparar y escribir dentro de una única transacción.
        with self._database() as db:
            current = self._load(db, run.run_id, identity)
            check_update(current, run, identity, recovering)
            saved = decode(encode(run))
            saved.version += 1
            updated = db.execute('''UPDATE runs SET version = ?, payload = ?
                WHERE run_id = ? AND workspace_id = ? AND requested_by = ? AND version = ?''',
                                 (saved.version, encode(saved), run.run_id, identity.workspace_id,
                                  identity.actor_id, run.version))
            if updated.rowcount != 1:
                raise ContractError('CONFLICT')
        return saved
