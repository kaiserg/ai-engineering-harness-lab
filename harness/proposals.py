"""Propuestas durables de pruebas: aprobación no equivale a despacho."""
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import UUID, uuid4

from .contracts import ContractError, Proposal
from .postgres_store import PostgresRunStore


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def uuid_text(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ContractError('INVALID_ARGUMENT') from None


class ProposalStore:
    def __init__(self, runs: PostgresRunStore):
        self.runs = runs
        # Migración del módulo aislada: no cambiar la versión que entienden workers
        # de lectura ya desplegados. Mismo schema, transacción y lock de migración.
        with runs._database() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('proposals:' + runs.schema,))
            db.execute('CREATE TABLE IF NOT EXISTS proposal_schema_version (singleton boolean PRIMARY KEY CHECK(singleton), version integer NOT NULL)')
            row = db.execute('SELECT version FROM proposal_schema_version WHERE singleton=true').fetchone()
            if row is None:
                db.execute((Path(__file__).parent/'migrations/proposals/001_proposals.sql').read_text())
                db.execute('INSERT INTO proposal_schema_version VALUES (true,1)')
                row = {'version':1}
            if row['version'] == 1:
                db.execute((Path(__file__).parent/'migrations/proposals/002_operations.sql').read_text())
                db.execute('UPDATE proposal_schema_version SET version=2')
                row = {'version':2}
            if row['version'] == 2:
                db.execute((Path(__file__).parent/'migrations/proposals/003_resolutions.sql').read_text())
                db.execute('UPDATE proposal_schema_version SET version=3')
                row = {'version':3}
            if row['version'] == 3:
                db.execute((Path(__file__).parent/'migrations/proposals/004_write_jobs.sql').read_text())
                db.execute('UPDATE proposal_schema_version SET version=4')
                row = {'version':4}
            if row['version'] == 4:
                db.execute((Path(__file__).parent/'migrations/proposals/005_write_traces.sql').read_text())
                db.execute('UPDATE proposal_schema_version SET version=5')
            elif row['version'] != 5:
                raise ContractError('STORAGE_SCHEMA_UNSUPPORTED')

    @staticmethod
    def _grant(identity, job='atlas-tests', repository='demo/atlas'):
        if job not in identity.jobs or repository not in identity.repositories or 'DEV' not in identity.projects:
            raise ContractError('FORBIDDEN')

    @staticmethod
    def _restore(row):
        proposal = Proposal(row['requested_by'], row['workspace_id'], row['canonical_payload'],
            row['expires_at'], row['status'], row['version'])
        if proposal.payload_hash != row['payload_hash']:
            raise ContractError('CORRUPT_STATE')
        return proposal

    def _load(self, db, identifier, identity):
        if not identity.roles & {'operator','approver'}:
            raise ContractError('FORBIDDEN')
        self._grant(identity)
        row = db.execute('''SELECT * FROM proposals WHERE proposal_id=%s AND workspace_id=%s
            AND (requested_by=%s OR %s) FOR UPDATE''',
            (uuid_text(identifier), identity.workspace_id, identity.actor_id, 'approver' in identity.roles)).fetchone()
        if row is None:
            raise ContractError('NOT_FOUND')
        self._restore(row)
        return row

    @staticmethod
    def _view(row, now):
        return {key: value.isoformat() if hasattr(value,'isoformat') else value
            for key,value in row.items() if key not in ('request_key','request_hash','canonical_payload')} | {
            'payload': json.loads(row['canonical_payload']),
            'effective_status': 'EXPIRED' if row['status'] in ('PENDING_APPROVAL','APPROVED') and row['expires_at'] <= now else row['status']}

    def get(self, identifier, identity):
        with self.runs._database() as db:
            row = self._load(db, identifier, identity)
            return self._view(row, db.execute('SELECT clock_timestamp() AS now').fetchone()['now'])

    def create(self, run_id, identity, request_key):
        run_id, request_key = uuid_text(run_id), uuid_text(request_key)
        if 'operator' not in identity.roles:
            raise ContractError('FORBIDDEN')
        self._grant(identity)
        request_hash = digest({'run_id':run_id, 'tool':'jenkins_request_tests', 'job':'atlas-tests'})
        with self.runs._database() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                (f'proposal:{self.runs.schema}:{identity.workspace_id}:{identity.actor_id}:{request_key}',))
            existing = db.execute('SELECT * FROM proposals WHERE workspace_id=%s AND requested_by=%s AND request_key=%s',
                (identity.workspace_id,identity.actor_id,request_key)).fetchone()
            now = db.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            if existing:
                if existing['request_hash'] != request_hash:
                    raise ContractError('CONFLICT')
                self._restore(existing)
                return self._view(existing,now)
            run = self.runs._load(db,run_id,identity,locked=True)
            # Solo evidencia terminal y enlazada, de un perfil de lectura conocido.
            from .profiles import parse_ci_profile
            from .evidence import finalize_ci_partial
            if run.status != 'COMPLETED' or parse_ci_profile(run.execution_profile) is None:
                raise ContractError('INCOMPLETE_EVIDENCE')
            from .core import authorize_run
            from .fakes import ReadOnlyPolicy
            authorize_run(run,identity,ReadOnlyPolicy())
            _, assessment = finalize_ci_partial(run)
            arguments = {'job':'atlas-tests','repository':'demo/atlas','revision':assessment['pr_revision']}
            expires = now + timedelta(minutes=30)
            proposal = Proposal.create(identity.actor_id,identity.workspace_id,'jenkins_request_tests',
                'atlas-tests',arguments,assessment['pr_revision'],expires)
            payload = json.loads(proposal.canonical_payload)
            payload['context'] = {'run_id':run_id,'run_version':run.version,
                'evidence_hash':digest({'results':[{'tool':r.tool_name,'source':r.source_id,'data':r.data} for r in run.results],
                    'failures':[{'tool':f.call.name,'arguments':f.call.arguments,'code':f.code} for f in run.tool_failures]}),
                'sources':assessment['sources'], 'policy_version':'proposal-tests-v1',
                'config_version':run.execution_profile}
            canonical = json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            identifier = str(uuid4())
            db.execute('''INSERT INTO proposals (proposal_id,run_id,workspace_id,requested_by,request_key,
                request_hash,canonical_payload,payload_hash,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (identifier,run_id,identity.workspace_id,identity.actor_id,request_key,request_hash,
                 canonical,sha256(canonical.encode()).hexdigest(),expires))
            return self._view(self._load(db,identifier,identity),now)

    def decide(self, identifier, identity, *, payload_hash, expected_version, decision):
        if (decision not in ('APPROVED','REJECTED') or type(expected_version) is not int or expected_version < 0
                or type(payload_hash) is not str or not re.fullmatch('[0-9a-f]{64}',payload_hash)):
            raise ContractError('INVALID_ARGUMENT')
        expired = False
        with self.runs._database() as db:
            row = self._load(db,identifier,identity)
            proposal = self._restore(row)
            if 'approver' not in identity.roles or identity.actor_id == row['requested_by']:
                raise ContractError('FORBIDDEN')
            now = db.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            if payload_hash != proposal.payload_hash:
                raise ContractError('CONFLICT')
            if row['status']=='EXPIRED' or (row['status'] != 'REJECTED' and row['expires_at'] <= now):
                if row['status']=='PENDING_APPROVAL':
                    db.execute("UPDATE proposals SET status='EXPIRED',version=version+1 WHERE proposal_id=%s",(row['proposal_id'],))
                expired = True
            elif (row['decided_by']==identity.actor_id and (
                    (row['status']==decision and row['version']==expected_version+1)
                    or (decision=='APPROVED' and row['status'] in ('CONSUMED','INVALIDATED')
                        and row['version']==expected_version+2))):
                # Conservar el reintento de la decisión aunque la autorización ya se consumió.
                # Se devuelve el estado actual; no se vuelve a aprobar ni extiende vigencia.
                return self._view(row,now)
            else:
                proposal.approve(identity,payload_hash,expected_version,now)  # Guardas comunes también para rechazo.
                db.execute('''UPDATE proposals SET status=%s,version=version+1,decided_by=%s,
                    decided_at=%s WHERE proposal_id=%s AND version=%s''',
                    (decision,identity.actor_id,now,row['proposal_id'],expected_version))
            if not expired:
                result = self._view(self._load(db,identifier,identity),now)
        if expired:
            raise ContractError('APPROVAL_EXPIRED')  # Después del commit de EXPIRED.
        return result
