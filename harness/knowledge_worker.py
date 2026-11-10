"""Consumidor local de ingestión: autoridad actual, lease fijo y candidatos aislados por intento."""
import argparse
import json
import time
from uuid import uuid4
from .chroma_vectors import ChromaVectors
from .contracts import ContractError
from .knowledge import Knowledge
from .knowledge_queue import KnowledgeQueue
from .postgres_config import local_dsn
from .postgres_store import PostgresRunStore
from .user_identity import UserIdentityStore


class KnowledgeWorker:
    def __init__(self,queue,*,worker_id=None,lease_seconds=90,vectors_factory=None):
        self.queue=queue
        self.worker_id=worker_id or 'knowledge-'+uuid4().hex
        self.lease_seconds=lease_seconds
        self.vectors_factory=vectors_factory or (lambda check:ChromaVectors(check))

    def run_once(self):
        claim=self.queue.claim(self.worker_id,lease_seconds=self.lease_seconds)
        if claim is None:return None
        check=lambda:self.queue.check(claim)
        source=self.queue.knowledge
        knowledge=Knowledge(source.runs,source.authenticate,self.vectors_factory(check))
        try:
            check()
            result=knowledge.rebuild(guard=lambda db,**values:self.queue.guard(db,claim,**values))
            return {'job_id':claim.job_id,'status':'DONE','attempts':claim.attempts,**result}
        except Exception as error:
            code=error.code if isinstance(error,ContractError) else 'INGEST_FAILED'
            owned=self.queue.fail(claim,code)
            return {'job_id':claim.job_id,'status':'INTERRUPTED' if not owned else 'ATTEMPT_FAILED','error':code}
        # BaseException / muerte real dejan RUNNING: otro proceso recupera tras vencer el lease.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor',required=True);parser.add_argument('--workspace',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    runs=PostgresRunStore(local_dsn(),schema='harness_users_lab')
    directory=UserIdentityStore(PostgresRunStore(local_dsn()))
    authenticate=lambda:directory.resolve(args.actor,args.workspace)
    knowledge=Knowledge(runs,authenticate)
    worker=KnowledgeWorker(KnowledgeQueue(knowledge))
    while True:
        result=worker.run_once()
        if result:print(json.dumps(result),flush=True)
        if args.once:break
        time.sleep(1)


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:pass
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'KNOWLEDGE_WORKER_FAILED'}))
        raise SystemExit(1) from None
