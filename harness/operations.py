"""Un despacho acotado por aprobación; incertidumbre se resuelve consultando, no reenviando."""
import json
from uuid import uuid4
from .contracts import ContractError
from .write_observability import phase
from .core import authorize_run
from .fakes import ReadOnlyPolicy
from .proposals import ProposalStore,digest,uuid_text


class OperationInterrupted(BaseException):
    pass


class Operations:
    def __init__(self,proposals,provider,principals,*,guard=None):
        self.guard=guard
        self.proposals,self.runs,self.provider,self.principals=proposals,proposals.runs,provider,dict(principals)

    def _check(self,db):
        return self.guard(db) if self.guard else None

    def _operator(self,identity):
        if self.principals.get(identity.actor_id)!=identity or 'operator' not in identity.roles:
            raise ContractError('FORBIDDEN')
        ProposalStore._grant(identity)

    @phase('authorize')
    def _valid(self,db,proposal,identity):
        self._operator(identity)
        self.proposals._restore(proposal)
        approver=self.principals.get(proposal['decided_by'])
        if (approver is None or 'approver' not in approver.roles or approver.actor_id==identity.actor_id
                or approver.workspace_id!=identity.workspace_id):
            raise ContractError('FORBIDDEN')
        ProposalStore._grant(approver)
        now=db.execute('SELECT clock_timestamp() AS now').fetchone()['now']
        if proposal['expires_at']<=now:raise ContractError('APPROVAL_EXPIRED')
        payload=json.loads(proposal['canonical_payload'])
        if (payload['actor']!=identity.actor_id or payload['workspace']!=identity.workspace_id
                or payload['tool']!='jenkins_request_tests' or payload['resource']!='atlas-tests'
                or payload['context']['policy_version']!='proposal-tests-v1'):
            raise ContractError('CONFIG_CHANGED')
        run=self.runs._load(db,proposal['run_id'],identity,locked=True)
        authorize_run(run,identity,ReadOnlyPolicy())
        evidence={'results':[{'tool':r.tool_name,'source':r.source_id,'data':r.data} for r in run.results],
            'failures':[{'tool':f.call.name,'arguments':f.call.arguments,'code':f.code} for f in run.tool_failures]}
        if (run.status!='COMPLETED' or run.version!=payload['context']['run_version']
                or run.execution_profile!=payload['context']['config_version']
                or digest(evidence)!=payload['context']['evidence_hash']):
            raise ContractError('STALE_PRECONDITION')
        return payload['arguments']

    @staticmethod
    def _event(db,row,actor):
        db.execute('INSERT INTO operation_events (operation_id,version,status,actor_id) VALUES (%s,%s,%s,%s)',
            (row['operation_id'],row['version'],row['status'],actor))

    def _transition(self,db,row,status,identity,*,receipt=None,code=None):
        self._check(db)
        updated=db.execute('''UPDATE operations SET status=%s,version=version+1,receipt=%s,outcome_code=%s,
            lease_until=CASE WHEN %s='DISPATCHING' THEN clock_timestamp()+interval '10 seconds' ELSE NULL END
            WHERE operation_id=%s AND version=%s RETURNING *''',
            (status,receipt,code,status,row['operation_id'],row['version'])).fetchone()
        if updated is None:raise ContractError('CONFLICT')
        self._event(db,updated,identity.actor_id)
        return updated

    def _load(self,db,identifier,identity):
        self._check(db)
        self._operator(identity)
        row=db.execute('''SELECT o.* FROM operations o JOIN proposals p ON p.proposal_id=o.proposal_id
            WHERE operation_id=%s AND p.workspace_id=%s AND p.requested_by=%s FOR UPDATE OF o''',
            (uuid_text(identifier),identity.workspace_id,identity.actor_id)).fetchone()
        if row is None:raise ContractError('NOT_FOUND')
        proposal=self.proposals._load(db,row['proposal_id'],identity)
        return row,proposal

    @staticmethod
    def _view(row):
        return {key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in row.items()}

    def get(self,identifier,identity):
        with self.runs._database() as db:
            row,_=self._load(db,identifier,identity)
            result=self._view(row)
            events=db.execute('SELECT version,status,actor_id,occurred_at FROM operation_events WHERE operation_id=%s ORDER BY version',
                (row['operation_id'],)).fetchall()
            result['events']=[self._view(event) for event in events]
            return result

    @phase('prepare',id_key='proposal.id')
    def prepare(self,proposal_id,identity):
        self._operator(identity)
        failure=None
        with self.runs._database() as db:
            proposal=self.proposals._load(db,proposal_id,identity)
            existing=db.execute('SELECT * FROM operations WHERE proposal_id=%s',(proposal['proposal_id'],)).fetchone()
            if existing:return self._view(existing)
            if proposal['status']!='APPROVED':raise ContractError('CONFLICT')
            arguments=self._valid(db,proposal,identity)
            if self.provider.revision(identity.workspace_id)!=arguments['revision']:
                db.execute("UPDATE proposals SET status='INVALIDATED',version=version+1 WHERE proposal_id=%s",(proposal['proposal_id'],))
                failure='STALE_PRECONDITION'
            else:
                identifier=str(uuid4())
                row=db.execute("INSERT INTO operations (operation_id,proposal_id,status) VALUES (%s,%s,'PREPARED') RETURNING *",
                    (identifier,proposal['proposal_id'])).fetchone()
                db.execute("UPDATE proposals SET status='CONSUMED',version=version+1 WHERE proposal_id=%s",(proposal['proposal_id'],))
                self._event(db,row,identity.actor_id)
                result=self._view(row)
        if failure:raise ContractError(failure)
        return result

    @phase('dispatch')
    def dispatch(self,identifier,identity,*,interrupt=None):
        with self.runs._database() as db:
            # Serializar reclamos por workspace/job, sin mantener esta transacción durante el efecto.
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(f'write:{self.runs.schema}:{identity.workspace_id}:atlas-tests',))
            row,proposal=self._load(db,identifier,identity)
            if row['status'] in ('SUCCEEDED','NOT_APPLIED'):return self._view(row)
            if row['status']!='PREPARED':raise ContractError('UNSAFE_RECOVERY')
            blocked=db.execute('''SELECT 1 FROM operations o JOIN proposals p ON p.proposal_id=o.proposal_id
                WHERE p.workspace_id=%s AND o.status IN ('DISPATCHING','UNKNOWN') LIMIT 1''',(identity.workspace_id,)).fetchone()
            if blocked:raise ContractError('OPERATION_BUSY')
            try:
                arguments=self._valid(db,proposal,identity)
            except ContractError as error:
                return self._view(self._transition(db,row,'NOT_APPLIED',identity,code=error.code))
            job_deadline=self._check(db)
            claimed=self._transition(db,row,'DISPATCHING',identity)
        # DISPATCHING y su evento ya están confirmados antes del I/O.
        if interrupt=='before-send':raise OperationInterrupted()
        try:
            if self.guard:
                with self.runs._database() as db:self._check(db)
            receipt=self.provider.request(identity,row['operation_id'],proposal['payload_hash'],arguments,
                min(value for value in (proposal['expires_at'],claimed['lease_until'],job_deadline) if value is not None))
            if interrupt=='after-send':raise OperationInterrupted()
            if receipt!='queue:'+row['operation_id']:raise ContractError('INVALID_TOOL_OUTPUT')
        except Exception as error:
            status='NOT_APPLIED' if isinstance(error,ContractError) and error.code in ('STALE_PRECONDITION','DISPATCH_EXPIRED') else 'UNKNOWN'
            code=error.code if status=='NOT_APPLIED' else 'UNKNOWN_EFFECT'
            return self._finish(identifier,identity,claimed['version'],status,code=code)
        return self._finish(identifier,identity,claimed['version'],'SUCCEEDED',receipt=receipt)

    def _finish(self,identifier,identity,version,status,*,receipt=None,code=None):
        with self.runs._database() as db:
            row,_=self._load(db,identifier,identity)
            if row['version']!=version or row['status'] not in ('DISPATCHING','UNKNOWN'):
                raise ContractError('CONFLICT')
            return self._view(self._transition(db,row,status,identity,receipt=receipt,code=code))

    @phase('lookup')
    def reconcile(self,identifier,identity):
        with self.runs._database() as db:
            row,proposal=self._load(db,identifier,identity)
            if row['status'] in ('SUCCEEDED','NOT_APPLIED'):return self._view(row)
            if row['status']=='DISPATCHING':
                now=db.execute('SELECT clock_timestamp() AS now').fetchone()['now']
                if row['lease_until']>now:raise ContractError('OPERATION_BUSY')
                row=self._transition(db,row,'UNKNOWN',identity,code='UNKNOWN_EFFECT')
            if row['status']!='UNKNOWN':raise ContractError('INVALID_TRANSITION')
        # Ni expiración de aprobación ni cambio de head impiden consultar un efecto ya enviado.
        receipt=self.provider.lookup(identity,row['operation_id'],proposal['payload_hash'])
        if receipt is None:return self._view(row)  # Ausencia no autoriza otro envío.
        if receipt!='queue:'+row['operation_id']:raise ContractError('INVALID_TOOL_OUTPUT')
        return self._finish(identifier,identity,row['version'],'SUCCEEDED',receipt=receipt)
