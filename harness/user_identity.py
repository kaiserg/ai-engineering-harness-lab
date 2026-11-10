"""Directorio y credenciales opacas locales. La pertenencia nunca viene del cliente."""
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import re
import secrets
from uuid import uuid4
from .contracts import ContractError,Identity
from .development import fixture_identity
from .proposals import uuid_text

TOKEN=re.compile(r'^hsu_[A-Za-z0-9_-]{43}$')
NAME=re.compile(r'^[a-z][a-z0-9-]{0,63}$')
ROLES=frozenset({'reader','operator','approver'})


def name(value):
    if type(value) is not str or not NAME.fullmatch(value):raise ContractError('INVALID_ARGUMENT')
    return value


def view(identity):
    return {key:sorted(value) if isinstance(value,frozenset) else value for key,value in asdict(identity).items()}


class UserIdentityStore:
    def __init__(self,runs):
        self.runs=runs
        with runs._database() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('users:'+runs.schema,))
            db.execute('CREATE TABLE IF NOT EXISTS user_schema_version (singleton boolean PRIMARY KEY CHECK(singleton),version integer NOT NULL)')
            row=db.execute('SELECT version FROM user_schema_version WHERE singleton=true').fetchone()
            if row is None:
                db.execute((Path(__file__).parent/'migrations/users/001_identity.sql').read_text())
                db.execute('INSERT INTO user_schema_version VALUES (true,1)')
            elif row['version']!=1:raise ContractError('STORAGE_SCHEMA_UNSUPPORTED')

    def bootstrap(self):
        """Fixtures explícitas; repetir no restaura cuentas/permisos deshabilitados."""
        from dataclasses import replace
        identities=[fixture_identity('operator'),fixture_identity('approver'),
            replace(fixture_identity('operator'),actor_id='reader-beta',workspace_id='workspace-beta')]
        with self.runs._database() as db:
            for identity in identities:
                db.execute('INSERT INTO user_accounts(actor_id) VALUES (%s) ON CONFLICT DO NOTHING',(identity.actor_id,))
                db.execute('''INSERT INTO user_memberships(actor_id,workspace_id,roles,projects,repositories,jobs)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (identity.actor_id,identity.workspace_id,sorted(identity.roles),sorted(identity.projects),
                     sorted(identity.repositories),sorted(identity.jobs)))
        return {'accounts_checked':len(identities),'existing_permissions_preserved':True}

    def set_membership(self,actor,workspace,roles,*,enabled=True):
        name(actor);name(workspace)
        if not isinstance(roles,(set,frozenset)) or not roles<=ROLES or type(enabled) is not bool:
            raise ContractError('INVALID_ARGUMENT')
        with self.runs._database() as db:
            row=db.execute('''UPDATE user_memberships SET roles=%s,enabled=%s
                WHERE actor_id=%s AND workspace_id=%s RETURNING actor_id''',
                (sorted(roles),enabled,actor,workspace)).fetchone()
            if row is None:raise ContractError('NOT_FOUND')

    def issue(self,actor,workspace):
        name(actor);name(workspace)
        token='hsu_'+secrets.token_urlsafe(32);identifier=str(uuid4())
        with self.runs._database() as db:
            row=db.execute('''SELECT 1 FROM user_memberships m JOIN user_accounts u USING(actor_id)
                WHERE m.actor_id=%s AND m.workspace_id=%s AND m.enabled AND u.enabled FOR UPDATE OF m,u''',
                (actor,workspace)).fetchone()
            if row is None:raise ContractError('FORBIDDEN')
            row=db.execute('''INSERT INTO user_sessions(session_id,token_hash,actor_id,workspace_id,expires_at)
                VALUES (%s,%s,%s,%s,clock_timestamp()+interval '1 hour') RETURNING expires_at''',
                (identifier,sha256(token.encode()).hexdigest(),actor,workspace)).fetchone()
        return {'session_id':identifier,'token':token,'expires_at':row['expires_at'].isoformat()}

    def authenticate(self,authorization,requested_workspace=None):
        if type(authorization) is not str or not authorization.startswith('Bearer '):raise ContractError('UNAUTHENTICATED')
        token=authorization[7:]
        if not TOKEN.fullmatch(token):raise ContractError('UNAUTHENTICATED')
        with self.runs._database() as db:
            row=db.execute('''SELECT m.actor_id,m.workspace_id,m.roles,m.projects,m.repositories,m.jobs
                FROM user_sessions s JOIN user_memberships m USING(actor_id,workspace_id)
                JOIN user_accounts u USING(actor_id)
                WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp()
                AND m.enabled AND u.enabled''',(sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:raise ContractError('UNAUTHENTICATED')
        if requested_workspace is not None and requested_workspace!=row['workspace_id']:raise ContractError('FORBIDDEN')
        return Identity(row['actor_id'],row['workspace_id'],*(frozenset(row[key]) for key in ('roles','projects','repositories','jobs')))

    def resolve(self,actor,workspace):
        """Permisos actuales para trabajo durable; no depende de una sesión del navegador."""
        name(actor);name(workspace)
        with self.runs._database() as db:
            row=db.execute('''SELECT m.* FROM user_memberships m JOIN user_accounts u USING(actor_id)
                WHERE m.actor_id=%s AND m.workspace_id=%s AND m.enabled AND u.enabled''',
                (actor,workspace)).fetchone()
        if row is None:raise ContractError('FORBIDDEN')
        return Identity(row['actor_id'],row['workspace_id'],*(frozenset(row[key]) for key in ('roles','projects','repositories','jobs')))

    def revoke(self,identifier):
        with self.runs._database() as db:
            row=db.execute('''UPDATE user_sessions SET revoked_at=COALESCE(revoked_at,clock_timestamp())
                WHERE session_id=%s RETURNING session_id''',(uuid_text(identifier),)).fetchone()
            if row is None:raise ContractError('NOT_FOUND')
