"""Revisión manual de evidencia autoritativa; nunca un override libre de UNKNOWN."""
import json
from .contracts import ContractError
from .write_observability import phase
from .jenkins_write_contract import validate_closure
from .proposals import digest,ProposalStore


class Resolutions:
    def __init__(self,operations):self.ops=operations;self.runs=operations.runs

    @staticmethod
    def _view(row,current_version):
        if row is None:return None
        payload=json.loads(row['canonical_payload'])
        if digest(payload)!=row['payload_hash']:raise ContractError('CORRUPT_STATE')
        result={key:value.isoformat() if hasattr(value,'isoformat') else value
            for key,value in row.items() if key!='canonical_payload'}
        return result|{'payload':payload,'effective_status':'STALE' if row['status']=='PENDING_REVIEW'
            and row['operation_version']!=current_version else row['status']}

    def get(self,identifier,operator):
        with self.runs._database() as db:
            op,_=self.ops._load(db,identifier,operator)
            row=db.execute('SELECT * FROM operation_resolutions WHERE operation_id=%s',(op['operation_id'],)).fetchone()
            return self._view(row,op['version'])

    @phase('resolution.prepare')
    def prepare(self,identifier,operator):
        with self.runs._database() as db:
            op,proposal=self.ops._load(db,identifier,operator)
            row=db.execute('SELECT * FROM operation_resolutions WHERE operation_id=%s',(op['operation_id'],)).fetchone()
            if row:return self._view(row,op['version'])
            if op['status']!='UNKNOWN':raise ContractError('INVALID_TRANSITION')
        proof=self.ops.provider.close_request(operator,op['operation_id'],proposal['payload_hash'])
        validate_closure(proof,op['operation_id'],proposal['payload_hash'])
        payload={'operation_id':op['operation_id'],'operation_version':op['version'],
            'workspace':operator.workspace_id,'requested_by':operator.actor_id,
            'proposal_hash':proposal['payload_hash'],'provider':'jenkins-write-fixture:v2','evidence':proof,
            'conclusion':'SUCCEEDED' if proof['receipt'] is not None else 'NOT_APPLIED'}
        canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        with self.runs._database() as db:
            current,_=self.ops._load(db,identifier,operator)
            row=db.execute('SELECT * FROM operation_resolutions WHERE operation_id=%s',(op['operation_id'],)).fetchone()
            if row:return self._view(row,current['version'])
            if current['status']!='UNKNOWN' or current['version']!=op['version']:raise ContractError('CONFLICT')
            db.execute('''INSERT INTO operation_resolutions (operation_id,operation_version,canonical_payload,payload_hash,requested_by)
                VALUES (%s,%s,%s,%s,%s)''',(op['operation_id'],op['version'],canonical,digest(payload),operator.actor_id))
            row=db.execute('SELECT * FROM operation_resolutions WHERE operation_id=%s',(op['operation_id'],)).fetchone()
            return self._view(row,current['version'])

    @phase('resolution.confirm')
    def confirm(self,identifier,operator,reviewer,*,payload_hash,expected_version):
        if type(expected_version) is not int or expected_version<0 or type(payload_hash) is not str:
            raise ContractError('INVALID_ARGUMENT')
        if (self.ops.principals.get(reviewer.actor_id)!=reviewer or 'approver' not in reviewer.roles
                or reviewer.actor_id==operator.actor_id or reviewer.workspace_id!=operator.workspace_id):
            raise ContractError('FORBIDDEN')
        ProposalStore._grant(reviewer)
        with self.runs._database() as db:
            op,proposal=self.ops._load(db,identifier,operator)
            row=db.execute('SELECT * FROM operation_resolutions WHERE operation_id=%s FOR UPDATE',(op['operation_id'],)).fetchone()
            if row is None:raise ContractError('NOT_FOUND')
            view=self._view(row,op['version'])
            if row['payload_hash']!=payload_hash:raise ContractError('CONFLICT')
            if row['status']=='CONFIRMED':
                if row['reviewed_by']!=reviewer.actor_id or row['version']!=expected_version+1:raise ContractError('CONFLICT')
                return view
            if (row['version']!=expected_version or op['status']!='UNKNOWN'
                    or row['operation_version']!=op['version']):raise ContractError('CONFLICT')
            payload=view['payload'];proof=payload['evidence']
            validate_closure(proof,op['operation_id'],proposal['payload_hash'])
            # Nueva consulta al mismo cierre durable; no basta un JSON aportado por quien revisa.
            # La llamada acotada solo verifica una evidencia ya cerrada/idempotente.
            observed=self.ops.provider.close_request(operator,op['operation_id'],proposal['payload_hash'])
            validate_closure(observed,op['operation_id'],proposal['payload_hash'])
            if observed!=proof:raise ContractError('CONFLICT')
            status='SUCCEEDED' if proof['receipt'] is not None else 'NOT_APPLIED'
            if payload['conclusion']!=status:raise ContractError('CORRUPT_STATE')
            db.execute('''UPDATE operation_resolutions SET status='CONFIRMED',version=version+1,
                reviewed_by=%s,reviewed_at=clock_timestamp() WHERE operation_id=%s''',(reviewer.actor_id,op['operation_id']))
            self.ops._transition(db,op,status,reviewer,receipt=proof['receipt'],code='MANUAL_PROVIDER_CLOSURE')
            row=db.execute('SELECT * FROM operation_resolutions WHERE operation_id=%s',(op['operation_id'],)).fetchone()
            return self._view(row,op['version']+1)
