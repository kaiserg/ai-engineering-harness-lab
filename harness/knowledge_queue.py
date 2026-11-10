"""Admisión durable y activación atómica del índice, protegida por lease/fencing."""
from dataclasses import dataclass
from uuid import uuid4
from .contracts import ContractError
from .knowledge import CONFIG,Knowledge
from .embeddings import provider_for
from .proposals import uuid_text


@dataclass(frozen=True)
class KnowledgeClaim:
    job_id:str
    workspace_id:str
    requested_by:str
    owner:str
    fencing_token:int
    attempts:int
    configuration:str=CONFIG


class KnowledgeQueue:
    def __init__(self,knowledge):self.knowledge=knowledge;self.runs=knowledge.runs

    @staticmethod
    def view(row):
        return {key:value.isoformat() if hasattr(value,'isoformat') else value for key,value in row.items()
                if key not in ('owner','fencing_token','lease_until')}

    def enqueue(self,request_key,*,configuration=None):
        configuration=configuration or self.knowledge.configuration
        provider_for(configuration)
        request_key=uuid_text(request_key)
        identity=self.knowledge.identity(True)
        with self.runs._database() as db:
            inventory=Knowledge.inventory(db,identity.workspace_id)
            previous=db.execute('''SELECT * FROM knowledge_jobs WHERE workspace_id=%s AND requested_by=%s AND request_key=%s''',
                (identity.workspace_id,identity.actor_id,request_key)).fetchone()
            if previous and previous['configuration']!=configuration:raise ContractError('CONFLICT')
            if previous:return self.view(previous)  # No reinicia plazos ni intentos.
            pending=db.execute("SELECT count(*) n FROM knowledge_jobs WHERE workspace_id=%s AND status IN ('PENDING','RUNNING')",(identity.workspace_id,)).fetchone()['n']
            if pending>=16:raise ContractError('QUEUE_FULL')
            row=db.execute('''INSERT INTO knowledge_jobs(job_id,workspace_id,requested_by,request_key,inventory_revision,configuration)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
                (str(uuid4()),identity.workspace_id,identity.actor_id,request_key,inventory['revision'],configuration)).fetchone()
        return self.view(row)

    def get(self,identifier):
        identity=self.knowledge.identity()
        with self.runs._database() as db:
            row=db.execute('''SELECT * FROM knowledge_jobs WHERE job_id=%s AND workspace_id=%s AND requested_by=%s''',
                (uuid_text(identifier),identity.workspace_id,identity.actor_id)).fetchone()
        if not row:raise ContractError('NOT_FOUND')
        return self.view(row)

    def recent(self):
        identity=self.knowledge.identity()
        with self.runs._database() as db:
            rows=db.execute("""SELECT *,COALESCE(lease_until>clock_timestamp(),false) AS lease_active,
                deadline_at<=clock_timestamp() AS deadline_expired FROM knowledge_jobs
                WHERE workspace_id=%s AND requested_by=%s ORDER BY created_at DESC,job_id LIMIT 50""",
                (identity.workspace_id,identity.actor_id)).fetchall()
        return [self.view(row) for row in rows]

    def claim(self,owner,*,lease_seconds=90):
        if type(lease_seconds) not in (int,float) or not 2<=lease_seconds<=90:raise ValueError('Invalid knowledge lease')
        identity=self.knowledge.identity(True)
        with self.runs._database() as db:
            # Un dueño vivo conserva su claim hasta el lease; guard impide activar tras el deadline.
            db.execute('''UPDATE knowledge_jobs SET status='FAILED',lease_until=NULL,
                last_error=CASE WHEN deadline_at<=clock_timestamp() THEN 'INGEST_DEADLINE' ELSE 'INGEST_ATTEMPTS_EXHAUSTED' END
                WHERE workspace_id=%s AND requested_by=%s AND (deadline_at<=clock_timestamp() OR attempts=3)
                AND (status='PENDING' OR status='RUNNING' AND lease_until<=clock_timestamp())''',
                (identity.workspace_id,identity.actor_id))
            row=db.execute('''SELECT * FROM knowledge_jobs WHERE workspace_id=%s AND requested_by=%s
                AND deadline_at>clock_timestamp() AND attempts<3
                AND (status='PENDING' OR status='RUNNING' AND lease_until<=clock_timestamp())
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1''',(identity.workspace_id,identity.actor_id)).fetchone()
            if not row:return None
            if row['index_id']:
                db.execute("UPDATE knowledge_indexes SET state='FAILED' WHERE index_id=%s AND state='BUILDING'",(row['index_id'],))
            row=db.execute('''UPDATE knowledge_jobs SET status='RUNNING',owner=%s,fencing_token=fencing_token+1,
                attempts=attempts+1,lease_until=LEAST(deadline_at,clock_timestamp()+%s*interval '1 second'),
                index_id=NULL,last_error=NULL WHERE job_id=%s RETURNING *''',(owner,lease_seconds,row['job_id'])).fetchone()
        return KnowledgeClaim(row['job_id'],row['workspace_id'],row['requested_by'],owner,row['fencing_token'],row['attempts'],row['configuration'])

    @staticmethod
    def guard(db,claim,*,index_id=None,complete=False):
        row=db.execute('''SELECT *,deadline_at>clock_timestamp() AS within_deadline,lease_until>clock_timestamp() AS within_lease
            FROM knowledge_jobs WHERE job_id=%s AND workspace_id=%s AND requested_by=%s
            AND status='RUNNING' AND owner=%s AND fencing_token=%s FOR UPDATE''',
            (claim.job_id,claim.workspace_id,claim.requested_by,claim.owner,claim.fencing_token)).fetchone()
        if row is None:raise ContractError('LEASE_LOST')
        if not row['within_deadline']:raise ContractError('INGEST_DEADLINE')
        if not row['within_lease']:raise ContractError('LEASE_LOST')
        inventory=db.execute('SELECT revision FROM knowledge_inventory WHERE workspace_id=%s',(claim.workspace_id,)).fetchone()
        if row['inventory_revision']!=inventory['revision']:raise ContractError('STALE_INVENTORY')
        provider_for(row['configuration'])
        if row['configuration']!=claim.configuration:raise ContractError('INCOMPATIBLE_INDEX')
        if complete and row['index_id']!=index_id:raise ContractError('CORRUPT_STATE')
        if index_id and not complete:db.execute('UPDATE knowledge_jobs SET index_id=%s WHERE job_id=%s',(index_id,claim.job_id))
        if complete:
            if not row['index_id']:raise ContractError('CORRUPT_STATE')
            db.execute("UPDATE knowledge_jobs SET status='DONE',lease_until=NULL WHERE job_id=%s",(claim.job_id,))

    def check(self,claim):
        identity=self.knowledge.identity(True)
        if (identity.actor_id,identity.workspace_id)!=(claim.requested_by,claim.workspace_id):raise ContractError('FORBIDDEN')
        with self.runs._database() as db:self.guard(db,claim)

    def fail(self,claim,code):
        # Autoridad de bookkeeping del proceso: una revocación debe poder terminar el job sin hacer I/O.
        retry=code in ('CHROMA_UNAVAILABLE','INDEX_DEADLINE','EMBEDDING_UNAVAILABLE')
        with self.runs._database() as db:
            row=db.execute('''UPDATE knowledge_jobs SET status=CASE
                WHEN %s AND attempts<3 AND deadline_at>clock_timestamp() THEN 'PENDING' ELSE 'FAILED' END,
                lease_until=NULL,last_error=%s WHERE job_id=%s AND workspace_id=%s AND requested_by=%s
                AND owner=%s AND fencing_token=%s AND status='RUNNING' AND lease_until>clock_timestamp()
                RETURNING index_id''',(retry,code,claim.job_id,claim.workspace_id,claim.requested_by,claim.owner,claim.fencing_token)).fetchone()
            if row and row['index_id']:
                db.execute("UPDATE knowledge_indexes SET state='FAILED' WHERE index_id=%s AND state='BUILDING'",(row['index_id'],))
        return row is not None
