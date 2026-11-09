"""Worker local de escrituras: un primer envío o recuperación por consulta, nunca replay ciego."""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import signal
from uuid import uuid4

from .contracts import ContractError
from .telemetry import span,carrier,annotate
from .metrics import safe
from .write_observability import worker_attempt
from .development import fixture_identity
from .operations import Operations,OperationInterrupted
from .write_queue import WriteQueue


class WriteWorker:
    def __init__(self,operations,identity,*,worker_id=None,lease_seconds=15,crash_at=None):
        if type(lease_seconds) not in (int,float) or not 2<=lease_seconds<=60:raise ValueError('Lease inválido')
        if crash_at not in (None,'after-claim','after-send'):raise ValueError('Punto de fallo inválido')
        self.ops,self.identity,self.queue=operations,identity,WriteQueue(operations)
        self.worker_id=worker_id or 'writer-'+str(uuid4())
        self.lease_seconds,self.crash_at=lease_seconds,crash_at

    async def run_once(self):
        claim=await asyncio.to_thread(self.queue.claim,self.worker_id,self.identity,lease_seconds=self.lease_seconds)
        if claim is None:return None
        with span('write.worker.attempt',root=True,links=(claim.producer_traceparent,claim.previous_traceparent),
                values={'operation.id':claim.operation_id,'job.attempt':claim.attempts,'write.phase':'worker'}), worker_attempt() as finish:
            value=carrier()
            if value:safe(lambda:self.queue.record_trace(claim,value))
            result=await self._run_claim(claim)
            finish(result)
            if type(result) is dict:annotate(**{'write.state':result.get('status'),'error.code':result.get('error')})
            return result

    async def _run_claim(self,claim):
        if self.crash_at=='after-claim':os._exit(17)  # Solo flag explícito de laboratorio.
        execution=asyncio.create_task(asyncio.to_thread(self._execute,claim))
        heartbeat=asyncio.create_task(self._heartbeat(claim))
        try:
            done,_=await asyncio.wait({execution,heartbeat},return_when=asyncio.FIRST_COMPLETED)
            if execution in done:return execution.result()
            heartbeat.result()
            return await execution
        except ContractError as error:
            return {'event':'lease_lost' if error.code=='LEASE_LOST' else 'worker_error','error':error.code}
        except Exception:
            return {'event':'worker_error','error':'INTERNAL_ERROR'}
        finally:
            heartbeat.cancel()
            if not execution.done():execution.cancel()
            await asyncio.gather(heartbeat,execution,return_exceptions=True)

    async def _heartbeat(self,claim):
        while True:
            await asyncio.sleep(self.lease_seconds/3)
            if not await asyncio.to_thread(self.queue.renew,claim,self.lease_seconds):return

    def _execute(self,claim):
        bound=Operations(self.ops.proposals,self.ops.provider,self.ops.principals,
            guard=lambda db:self.queue.guard(db,claim))
        try:
            current=bound.get(claim.operation_id,self.identity)
            if current['status']=='PREPARED':
                result=bound.dispatch(claim.operation_id,self.identity,
                    interrupt='after-send' if self.crash_at=='after-send' else claim.interrupt)
                if result['status']=='UNKNOWN':result=bound.reconcile(claim.operation_id,self.identity)
            elif current['status'] in ('UNKNOWN','DISPATCHING'):
                result=bound.reconcile(claim.operation_id,self.identity)
            else:result=current
            return self.queue.finish(claim,status='DONE' if result['status'] in ('SUCCEEDED','NOT_APPLIED') else 'NEEDS_REVIEW',
                result=result['status'],error=result.get('outcome_code'))
        except OperationInterrupted:
            if self.crash_at=='after-send':os._exit(17)
            current=bound.get(claim.operation_id,self.identity)
            return self.queue.finish(claim,status='PENDING',result=current['status'],
                error='SIMULATED_INTERRUPTION',not_before=datetime.fromisoformat(current['lease_until']))
        except ContractError as error:
            if error.code=='LEASE_LOST':raise
            current=bound.get(claim.operation_id,self.identity)
            if current['status'] in ('SUCCEEDED','NOT_APPLIED'):
                return self.queue.finish(claim,status='DONE',result=current['status'],error=current.get('outcome_code'))
            if error.code=='OPERATION_BUSY':
                until=datetime.fromisoformat(current['lease_until']) if current.get('lease_until') else None
                return self.queue.finish(claim,status='PENDING',result=current['status'],error=error.code,not_before=until)
            return self.queue.finish(claim,status='NEEDS_REVIEW',result=current['status'],error=error.code)


async def main():
    from .adapters.mcp_jenkins_write import MCPJenkinsWrite
    from .postgres_config import local_dsn,ROOT
    from .postgres_store import PostgresRunStore
    from .proposals import ProposalStore
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema',default='harness_lab')
    parser.add_argument('--provider-db',type=Path,default=ROOT/'data/runtime/jenkins-write-simulator.sqlite3')
    parser.add_argument('--lease-seconds',type=float,default=15)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--crash-at',choices=['after-claim','after-send'])
    args=parser.parse_args()
    if args.crash_at and not args.once:parser.error('--crash-at requiere --once')
    operator,approver=fixture_identity('operator'),fixture_identity('approver')
    ops=Operations(ProposalStore(PostgresRunStore(local_dsn(),schema=args.schema)),MCPJenkinsWrite(operator,args.provider_db),
        {operator.actor_id:operator,approver.actor_id:approver})
    worker=WriteWorker(ops,operator,lease_seconds=args.lease_seconds,crash_at=args.crash_at)
    from .telemetry import configure
    from .write_observability import start
    configure('harness-write-worker')
    start(9102,lambda:worker.queue.snapshot(operator))
    stop=asyncio.Event();loop=asyncio.get_running_loop()
    for name in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(name,stop.set)
    while not stop.is_set():
        try:result=await worker.run_once()
        except ContractError as error:result={'event':'worker_error','error':error.code}
        if result:print(json.dumps(result),flush=True)
        if args.once:return
        try:await asyncio.wait_for(stop.wait(),timeout=.5)
        except TimeoutError:pass


if __name__=='__main__':
    try:asyncio.run(main())
    except (ContractError,ValueError):
        print(json.dumps({'error':'WRITE_WORKER_UNAVAILABLE'}))
        raise SystemExit(1) from None
