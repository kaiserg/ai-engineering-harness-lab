"""Proveedor de escritura ficticio, con commit independiente del ledger PostgreSQL."""
from contextlib import contextmanager, closing
import json
from datetime import datetime,timezone
from pathlib import Path
import sqlite3
from uuid import uuid4
from .security_lifecycle import atomic_json

from .contracts import ContractError
from .proposals import ProposalStore, uuid_text, digest


class JenkinsSimulator:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        marker=self.path.with_name(self.path.name+'.identity')
        if marker.exists() and not self.path.exists():raise ContractError('SIMULATOR_STATE_LOST')
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS heads (workspace TEXT PRIMARY KEY, revision TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS requests (operation_id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                actor TEXT NOT NULL,payload_hash TEXT NOT NULL,arguments TEXT NOT NULL,receipt TEXT NOT NULL)''')

        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS closures (operation_id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                actor TEXT NOT NULL,payload_hash TEXT NOT NULL,receipt TEXT,closed_at TEXT NOT NULL)''')
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS provider_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), provider_id TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO provider_identity VALUES (1,?)',(str(uuid4()),))
            provider_id=db.execute('SELECT provider_id FROM provider_identity WHERE singleton=1').fetchone()['provider_id']
        if marker.exists():
            try:
                if json.loads(marker.read_text())!={'provider_id':provider_id}:raise ValueError()
            except (ValueError,OSError):raise ContractError('SIMULATOR_STATE_LOST') from None
        else:atomic_json(marker,{'provider_id':provider_id})
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        try:
            with closing(sqlite3.connect(self.path,timeout=3)) as db:
                db.row_factory=sqlite3.Row
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    yield db
        except sqlite3.Error:
            raise ContractError('SIMULATOR_UNAVAILABLE') from None

    def revision(self,workspace):
        with self._db() as db:
            row=db.execute('SELECT revision FROM heads WHERE workspace=?',(workspace,)).fetchone()
            return row['revision'] if row else 'revision-A'

    def set_revision(self,workspace,revision):
        if revision not in ('revision-A','revision-B'):
            raise ContractError('INVALID_ARGUMENT')
        with self._db() as db:
            db.execute('INSERT INTO heads VALUES (?,?) ON CONFLICT(workspace) DO UPDATE SET revision=excluded.revision',(workspace,revision))

    @staticmethod
    def authorize(identity):
        ProposalStore._grant(identity)
        if 'operator' not in identity.roles:
            raise ContractError('FORBIDDEN')

    def lookup(self,identity,operation_id,payload_hash):
        self.authorize(identity)
        with self._db() as db:
            row=db.execute('SELECT * FROM requests WHERE operation_id=?',(uuid_text(operation_id),)).fetchone()
            if row is None:return None
            if (row['workspace'],row['actor'],row['payload_hash']) != (identity.workspace_id,identity.actor_id,payload_hash):
                raise ContractError('FORBIDDEN')
            return row['receipt']

    def request(self,identity,operation_id,payload_hash,arguments,not_after):
        self.authorize(identity);operation_id=uuid_text(operation_id)
        if (type(arguments) is not dict or set(arguments)!={'job','repository','revision'}
                or arguments['job']!='atlas-tests' or arguments['repository']!='demo/atlas'
                or arguments['revision'] not in ('revision-A','revision-B')):
            raise ContractError('INVALID_ARGUMENT')
        with self._db() as db:
            old=db.execute('SELECT * FROM requests WHERE operation_id=?',(operation_id,)).fetchone()
            canonical=json.dumps(arguments,sort_keys=True)
            if old:
                if (old['workspace'],old['actor'],old['payload_hash'],old['arguments']) != (identity.workspace_id,identity.actor_id,payload_hash,canonical):
                    raise ContractError('CONFLICT')
                return old['receipt']
            closed=db.execute('SELECT * FROM closures WHERE operation_id=?',(operation_id,)).fetchone()
            if closed is not None:raise ContractError('OPERATION_CLOSED')
            if not isinstance(not_after,datetime) or not_after.utcoffset() is None:
                raise ContractError('INVALID_ARGUMENT')
            if datetime.now(timezone.utc)>=not_after:
                raise ContractError('DISPATCH_EXPIRED')
            head=db.execute('SELECT revision FROM heads WHERE workspace=?',(identity.workspace_id,)).fetchone()
            if (head['revision'] if head else 'revision-A') != arguments['revision']:
                raise ContractError('STALE_PRECONDITION')  # Comprobación y efecto en una transacción del proveedor.
            receipt='queue:'+operation_id
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?)',
                (operation_id,identity.workspace_id,identity.actor_id,payload_hash,canonical,receipt))
            return receipt

    def close_request(self,identity,operation_id,payload_hash):
        self.authorize(identity);operation_id=uuid_text(operation_id)
        with self._db() as db:
            row=db.execute('SELECT * FROM closures WHERE operation_id=?',(operation_id,)).fetchone()
            if row is None:
                request=db.execute('SELECT * FROM requests WHERE operation_id=?',(operation_id,)).fetchone()
                if request and (request['workspace'],request['actor'],request['payload_hash']) != (identity.workspace_id,identity.actor_id,payload_hash):
                    raise ContractError('FORBIDDEN')
                receipt=request['receipt'] if request else None
                db.execute('INSERT INTO closures VALUES (?,?,?,?,?,?)',(operation_id,identity.workspace_id,identity.actor_id,
                    payload_hash,receipt,datetime.now(timezone.utc).isoformat()))
                row=db.execute('SELECT * FROM closures WHERE operation_id=?',(operation_id,)).fetchone()
            if (row['workspace'],row['actor'],row['payload_hash']) != (identity.workspace_id,identity.actor_id,payload_hash):
                raise ContractError('FORBIDDEN')
            data=dict(row)
            return {'operation_id':operation_id,'payload_hash':payload_hash,'receipt':row['receipt'],
                'closed':True,'closed_at':row['closed_at'],'evidence_id':'closure:'+digest(data)}
