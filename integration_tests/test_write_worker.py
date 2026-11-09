"""Cola independiente, fencing y recuperación del ledger de escritura."""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import test_operations as fixtures
from harness.contracts import ContractError
from harness.operations import Operations
from harness.write_queue import WriteQueue
from harness.write_worker import WriteWorker
from harness.resolutions import Resolutions

ROOT=Path(__file__).resolve().parents[1]


class WriteWorkerTests(unittest.IsolatedAsyncioTestCase):
    code=fixtures.OperationTests.code
    decide=fixtures.OperationTests.decide
    approved=fixtures.OperationTests.approved
    effects=fixtures.OperationTests.effects
    asyncTearDown=fixtures.OperationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.OperationTests.asyncSetUp(self)
        self.queue=WriteQueue(self.ops)
        self.op=self.approved()

    async def wait_job_expiry(self):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            with self.runs._database() as db:
                row=db.execute('SELECT lease_until<=clock_timestamp() AS expired FROM write_jobs WHERE operation_id=%s',
                    (self.op['operation_id'],)).fetchone()
            if row and row['expired']:return
            await asyncio.sleep(.1)
        self.fail('El reloj de PostgreSQL no confirmó el vencimiento.')

    async def test_enqueue_is_unique_and_does_not_send_without_worker(self):
        first=self.queue.enqueue(self.op['operation_id'],self.operator)
        second=self.queue.enqueue(self.op['operation_id'],self.operator)
        self.assertEqual(first,second);self.assertEqual(first['status'],'PENDING')
        self.assertEqual(self.effects(),0)
        self.code('CONFLICT',lambda:self.queue.enqueue(self.op['operation_id'],self.operator,interrupt='after-send'))
        result=await WriteWorker(self.ops,self.operator).run_once()
        self.assertEqual((result['status'],result['result_status']),('DONE','SUCCEEDED'))
        self.assertEqual(self.effects(),1)
        self.assertIsNone(await WriteWorker(self.ops,self.operator).run_once())

    async def test_two_workers_claim_one_job_and_one_effect(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        values=await asyncio.gather(WriteWorker(self.ops,self.operator).run_once(),WriteWorker(self.ops,self.operator).run_once())
        self.assertEqual(sum(value is not None for value in values),1)
        self.assertEqual(self.effects(),1)

    async def test_expired_owner_cannot_dispatch_or_finish_after_new_claim(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        old=self.queue.claim('old',self.operator,lease_seconds=2)
        await self.wait_job_expiry()
        new=self.queue.claim('new',self.operator,lease_seconds=15)
        self.assertGreater(new.fencing_token,old.fencing_token)
        stale=Operations(self.store,self.provider,self.principals,guard=lambda db:self.queue.guard(db,old))
        self.code('LEASE_LOST',lambda:stale.dispatch(self.op['operation_id'],self.operator))
        self.code('LEASE_LOST',lambda:self.queue.finish(old,status='DONE'))
        current=Operations(self.store,self.provider,self.principals,guard=lambda db:self.queue.guard(db,new))
        self.assertEqual(current.dispatch(self.op['operation_id'],self.operator)['status'],'SUCCEEDED')
        self.queue.finish(new,status='DONE',result='SUCCEEDED')
        self.assertEqual(self.effects(),1)

    async def test_heartbeat_keeps_claim_alive_during_long_validation(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        original=Operations._valid
        def slow(service,*args):
            time.sleep(2.2)
            return original(service,*args)
        # La validación bloquea la fila del lease: usar lease de 4s y comprobar renovación fuera de ella.
        with patch.object(Operations,'_valid',slow):
            result=await WriteWorker(self.ops,self.operator,lease_seconds=4).run_once()
        self.assertEqual(result['status'],'DONE')
        self.assertEqual(self.effects(),1)

    async def test_unknown_parks_for_review_and_manual_resolution_finishes_job(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        with patch.object(self.provider,'request',side_effect=TimeoutError),patch.object(
                self.provider,'lookup',wraps=self.provider.lookup) as lookup:
            result=await WriteWorker(self.ops,self.operator).run_once()
        self.assertEqual(lookup.call_count,1)
        with patch.object(self.provider,'request',side_effect=AssertionError('must not resend')),patch.object(
                self.provider,'lookup',wraps=self.provider.lookup) as lookup:
            # La cola ya está aparcada: un ciclo inactivo tampoco debe hacer polling.
            self.assertIsNone(await WriteWorker(self.ops,self.operator).run_once())
        lookup.assert_not_called()
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertEqual(self.effects(),0)
        resolutions=Resolutions(self.ops);review=resolutions.prepare(self.op['operation_id'],self.operator)
        resolutions.confirm(self.op['operation_id'],self.operator,self.approver,payload_hash=review['payload_hash'],expected_version=0)
        await WriteWorker(self.ops,self.operator).run_once()
        self.assertEqual(self.queue.get(self.op['operation_id'],self.operator)['status'],'DONE')
        self.assertEqual(self.effects(),0)

    async def test_revoked_approval_before_worker_produces_no_effect(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        revoked=replace(self.approver,jobs=frozenset())
        ops=Operations(self.store,self.provider,{**self.principals,self.approver.actor_id:revoked})
        result=await WriteWorker(ops,self.operator).run_once()
        self.assertEqual((result['status'],result['result_status']),('DONE','NOT_APPLIED'))
        self.assertEqual(self.effects(),0)

    async def test_process_death_after_effect_recovers_using_lookup_only(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        args=[sys.executable,'-m','harness.write_worker','--once','--schema',self.schema,
            '--provider-db',str(self.provider.path),'--lease-seconds','2']
        dead=await asyncio.to_thread(subprocess.run,args+['--crash-at','after-send'],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(dead.returncode,17,dead.stderr)
        self.assertEqual(self.effects(),1)
        await self.wait_job_expiry()
        # Primero puede diferir la consulta hasta el lease del ledger (10s), nunca enviar otra vez.
        worker=WriteWorker(self.ops,self.operator)
        deadline=time.monotonic()+30
        with patch.object(self.provider,'request',side_effect=AssertionError('must not resend')) as request:
            while time.monotonic()<deadline:
                await worker.run_once()
                if self.queue.get(self.op['operation_id'],self.operator)['status']=='DONE':break
                await asyncio.sleep(.2)
        request.assert_not_called()
        self.assertEqual(self.queue.get(self.op['operation_id'],self.operator)['result_status'],'SUCCEEDED')
        self.assertEqual(self.effects(),1)

    async def test_process_death_before_ledger_claim_allows_first_send_only(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        args=[sys.executable,'-m','harness.write_worker','--once','--schema',self.schema,
            '--provider-db',str(self.provider.path),'--lease-seconds','2','--crash-at','after-claim']
        dead=await asyncio.to_thread(subprocess.run,args,cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(dead.returncode,17,dead.stderr)
        self.assertEqual(self.ops.get(self.op['operation_id'],self.operator)['status'],'PREPARED')
        await self.wait_job_expiry()
        result=await WriteWorker(self.ops,self.operator).run_once()
        self.assertEqual(result['result_status'],'SUCCEEDED');self.assertEqual(self.effects(),1)

    async def test_claim_attempts_are_bounded_and_enqueue_retry_never_resets_them(self):
        first=self.queue.enqueue(self.op['operation_id'],self.operator)
        for _ in range(5):
            claim=self.queue.claim('test',self.operator)
            self.queue.finish(claim,status='PENDING')
        self.assertIsNone(self.queue.claim('sixth',self.operator))
        row=self.queue.enqueue(self.op['operation_id'],self.operator)
        self.assertEqual((row['job_id'],row['attempts'],row['status']),
            (first['job_id'],5,'NEEDS_REVIEW'))
        self.assertEqual(self.effects(),0)

    async def test_queue_admission_is_bounded_and_idempotent_at_capacity(self):
        from uuid import uuid4
        first=self.queue.enqueue(self.op['operation_id'],self.operator)
        for index in range(16):
            proposal=self.store.create(self.run.run_id,self.operator,str(uuid4()))
            self.store.decide(proposal['proposal_id'],self.approver,payload_hash=proposal['payload_hash'],expected_version=0,decision='APPROVED')
            operation=self.ops.prepare(proposal['proposal_id'],self.operator)
            if index<15:self.queue.enqueue(operation['operation_id'],self.operator)
            else:self.code('QUEUE_FULL',lambda:self.queue.enqueue(operation['operation_id'],self.operator))
        self.assertEqual(self.queue.enqueue(self.op['operation_id'],self.operator),first)
        self.assertEqual(self.effects(),0)

    async def test_sigterm_finishes_current_effect_and_stops_claiming(self):
        import signal
        self.queue.enqueue(self.op['operation_id'],self.operator)
        process=None
        try:
            # Retener solo el lock de recurso: el hijo reclama y queda realmente en curso,
            # antes del envío. Liberarlo después de SIGTERM evita depender de una carrera de timing.
            with self.runs._database() as db:
                db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                    (f'write:{self.runs.schema}:{self.operator.workspace_id}:atlas-tests',))
                process=subprocess.Popen([sys.executable,'-m','harness.write_worker','--schema',self.schema,
                    '--provider-db',str(self.provider.path)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    row=self.queue.get(self.op['operation_id'],self.operator)
                    if row['status']=='RUNNING':break
                    await asyncio.sleep(.05)
                else:self.fail('El worker no reclamó el trabajo')
                process.send_signal(signal.SIGTERM)
            _,errors=await asyncio.to_thread(process.communicate,timeout=20)
            self.assertEqual(process.returncode,0,errors)
            self.assertEqual(self.ops.get(self.op['operation_id'],self.operator)['status'],'SUCCEEDED')
            self.assertEqual(self.queue.get(self.op['operation_id'],self.operator)['status'],'DONE')
            self.assertEqual(self.effects(),1)
        finally:
            if process is not None and process.poll() is None:
                process.kill();await asyncio.to_thread(process.communicate,timeout=10)
