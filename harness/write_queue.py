"""Cola de envíos; el ledger sigue siendo la autoridad sobre el efecto."""
from dataclasses import dataclass
from uuid import uuid4
from .contracts import ContractError
from .proposals import uuid_text


@dataclass(frozen=True)
class WriteClaim:
    job_id:str
    operation_id:str
    owner:str
    fencing_token:int
    interrupt:str|None
    attempts:int=0
    producer_traceparent:str|None=None
    previous_traceparent:str|None=None


class WriteQueue:
    def __init__(self,operations):self.ops=operations;self.runs=operations.runs

    @staticmethod
    def view(row):
        return {key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in row.items()
            if key not in ('owner','fencing_token','producer_traceparent','previous_traceparent')}

    def get(self,identifier,identity):
        with self.runs._database() as db:
            op,_=self.ops._load(db,identifier,identity)
            row=db.execute('SELECT * FROM write_jobs WHERE operation_id=%s',(op['operation_id'],)).fetchone()
            return self.view(row) if row else None

    def enqueue(self,identifier,identity,*,interrupt=None):
        if interrupt not in (None,'before-send','after-send'):raise ContractError('INVALID_ARGUMENT')
        with self.runs._database() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',(f'write-admit:{self.runs.schema}:{identity.workspace_id}:{identity.actor_id}',))
            op,_=self.ops._load(db,identifier,identity)
            row=db.execute('SELECT * FROM write_jobs WHERE operation_id=%s',(op['operation_id'],)).fetchone()
            if row:
                if row['interrupt']!=interrupt:raise ContractError('CONFLICT')
                return self.view(row)
            if op['status']!='PREPARED':raise ContractError('INVALID_TRANSITION')
            count=db.execute('''SELECT count(*) AS n FROM write_jobs j JOIN operations o ON o.operation_id=j.operation_id
                JOIN proposals p ON p.proposal_id=o.proposal_id WHERE p.workspace_id=%s AND p.requested_by=%s
                AND j.status IN ('PENDING','RUNNING')''',(identity.workspace_id,identity.actor_id)).fetchone()['n']
            if count>=16:raise ContractError('QUEUE_FULL')
            from .telemetry import carrier
            row=db.execute('''INSERT INTO write_jobs(job_id,operation_id,interrupt,producer_traceparent) VALUES (%s,%s,%s,%s) RETURNING *''',
                (str(uuid4()),op['operation_id'],interrupt,carrier())).fetchone()
            return self.view(row)

    def claim(self,owner,identity,*,lease_seconds=15):
        if type(lease_seconds) not in (int,float) or not 2<=lease_seconds<=60:raise ValueError('Lease de escritura inválido')
        self.ops._operator(identity)
        with self.runs._database() as db:
            # Sin efecto pendiente: sincronizar trabajos aparcados tras resolución manual/CLI.
            db.execute('''UPDATE write_jobs j SET status='DONE',lease_until=NULL,fencing_token=fencing_token+1,
                result_status=o.status,last_error=o.outcome_code,updated_at=clock_timestamp()
                FROM operations o JOIN proposals p ON p.proposal_id=o.proposal_id
                WHERE j.operation_id=o.operation_id AND p.workspace_id=%s AND p.requested_by=%s
                AND o.status IN ('SUCCEEDED','NOT_APPLIED') AND j.status<>'DONE'
                AND (j.status<>'RUNNING' OR j.lease_until<=clock_timestamp())''',(identity.workspace_id,identity.actor_id))
            db.execute('''UPDATE write_jobs j SET status='NEEDS_REVIEW',lease_until=NULL,
                last_error='WRITE_ATTEMPTS_EXHAUSTED',updated_at=clock_timestamp()
                FROM operations o JOIN proposals p ON p.proposal_id=o.proposal_id
                WHERE j.operation_id=o.operation_id AND p.workspace_id=%s AND p.requested_by=%s
                AND j.attempts=5 AND (j.status='PENDING' OR j.status='RUNNING' AND j.lease_until<=clock_timestamp())''',
                (identity.workspace_id,identity.actor_id))
            row=db.execute('''SELECT j.* FROM write_jobs j JOIN operations o ON o.operation_id=j.operation_id
                JOIN proposals p ON p.proposal_id=o.proposal_id
                WHERE p.workspace_id=%s AND p.requested_by=%s AND j.attempts<5
                AND j.not_before<=clock_timestamp() AND (j.status='PENDING' OR j.status='RUNNING' AND j.lease_until<=clock_timestamp())
                AND (o.status<>'PREPARED' OR NOT EXISTS (SELECT 1 FROM operations blocked JOIN proposals bp ON bp.proposal_id=blocked.proposal_id
                    WHERE bp.workspace_id=p.workspace_id AND blocked.status IN ('DISPATCHING','UNKNOWN')))
                ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1''',(identity.workspace_id,identity.actor_id)).fetchone()
            if row is None:return None
            row=db.execute('''UPDATE write_jobs SET status='RUNNING',owner=%s,fencing_token=fencing_token+1,
                attempts=attempts+1,lease_until=clock_timestamp()+%s*interval '1 second',updated_at=clock_timestamp()
                WHERE job_id=%s RETURNING *''',(owner,lease_seconds,row['job_id'])).fetchone()
            return WriteClaim(row['job_id'],row['operation_id'],owner,row['fencing_token'],row['interrupt'],row['attempts'],
                row['producer_traceparent'],row['previous_traceparent'])

    @staticmethod
    def guard(db,claim):
        row=db.execute('''SELECT lease_until FROM write_jobs WHERE job_id=%s AND status='RUNNING'
            AND operation_id=%s AND owner=%s AND fencing_token=%s AND lease_until>clock_timestamp() FOR UPDATE''',
            (claim.job_id,claim.operation_id,claim.owner,claim.fencing_token)).fetchone()
        if row is None:raise ContractError('LEASE_LOST')
        return row['lease_until']

    def renew(self,claim,lease_seconds):
        with self.runs._database() as db:
            row=db.execute('SELECT * FROM write_jobs WHERE job_id=%s FOR UPDATE',(claim.job_id,)).fetchone()
            if row and row['owner']==claim.owner and row['fencing_token']==claim.fencing_token and row['status']!='RUNNING':return False
            self.guard(db,claim)
            db.execute("UPDATE write_jobs SET lease_until=clock_timestamp()+%s*interval '1 second' WHERE job_id=%s",
                (lease_seconds,claim.job_id))
            return True

    def finish(self,claim,*,status,result=None,error=None,not_before=None):
        if status not in ('DONE','NEEDS_REVIEW','PENDING'):raise ValueError('Estado de trabajo inválido')
        with self.runs._database() as db:
            self.guard(db,claim)
            row=db.execute('''UPDATE write_jobs SET status=%s,lease_until=NULL,result_status=%s,last_error=%s,
                not_before=COALESCE(%s,clock_timestamp()),updated_at=clock_timestamp() WHERE job_id=%s RETURNING *''',
                (status,result,error,not_before,claim.job_id)).fetchone()
            return self.view(row)

    def record_trace(self,claim,value):
        from .telemetry import carrier
        value=carrier(value)
        if value is None:return
        with self.runs._database() as db:
            self.guard(db,claim)
            db.execute('UPDATE write_jobs SET previous_traceparent=%s WHERE job_id=%s',(value,claim.job_id))

    def snapshot(self,identity):
        self.ops._operator(identity)
        with self.runs._database() as db:
            rows=db.execute('''WITH scoped AS (
                SELECT o.status AS operation_status,j.status AS job_status,j.lease_until,j.created_at
                FROM operations o JOIN proposals p ON p.proposal_id=o.proposal_id
                LEFT JOIN write_jobs j ON j.operation_id=o.operation_id
                WHERE p.workspace_id=%s AND p.requested_by=%s)
                SELECT 'operations' AS kind,operation_status AS status,count(*)::bigint AS value FROM scoped GROUP BY operation_status
                UNION ALL SELECT 'jobs',job_status,count(*)::bigint FROM scoped WHERE job_status IS NOT NULL GROUP BY job_status
                UNION ALL SELECT 'expired','value',count(*)::bigint FROM scoped WHERE job_status='RUNNING' AND lease_until<=clock_timestamp()
                UNION ALL SELECT 'oldest','value',COALESCE(max(GREATEST(0,extract(epoch FROM clock_timestamp()-created_at))),0)
                    FROM scoped WHERE job_status='PENDING'
                ''',(identity.workspace_id,identity.actor_id)).fetchall()
        result={'jobs':{},'operations':{},'expired_leases':0,'oldest_pending_seconds':0.0}
        for row in rows:
            if row['kind'] in ('jobs','operations'):result[row['kind']][row['status']]=int(row['value'])
            elif row['kind']=='expired':result['expired_leases']=int(row['value'])
            else:result['oldest_pending_seconds']=float(row['value'])
        return result
