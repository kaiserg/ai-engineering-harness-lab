"""Servicio de escritura: permisos antes del envío, autoridad de recuperación y fencing."""
import asyncio
from time import monotonic
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4
import test_authenticated_proposals as fixtures
from lease_wait import wait_for_expired_lease
from harness.contracts import ContractError
from harness.user_write_worker import create_worker
from harness.operations import Operations


class UserWriteWorkerTests(unittest.IsolatedAsyncioTestCase):
    cleanup=fixtures.AuthenticatedProposalTests.cleanup
    headers=fixtures.AuthenticatedProposalTests.headers
    start=fixtures.AuthenticatedProposalTests.start
    worker=fixtures.AuthenticatedProposalTests.worker
    post=fixtures.AuthenticatedProposalTests.post
    decide=fixtures.AuthenticatedProposalTests.decide
    get=fixtures.AuthenticatedProposalTests.get
    asyncSetUp=fixtures.AuthenticatedProposalTests.asyncSetUp

    def writer(self,actor='reader-demo',workspace='workspace-alpha',**kwargs):
        return create_worker(self.runs,self.directory,actor,workspace,self.path,**kwargs)

    def prepare(self):
        self.decide()
        return self.post('/proposals/'+self.item['proposal_id']+'/operation',{})['operation']['operation_id']

    def enqueue(self,identifier,**body):return self.post('/operations/'+identifier+'/dispatch',body,expected=202)

    def effects(self):
        with closing(sqlite3.connect(self.path)) as db:return db.execute('SELECT count(*) FROM requests').fetchone()[0]

    async def recover(self,writer,identifier):
        deadline=monotonic()+30
        while monotonic()<deadline:
            result=await writer.run_once()
            if result is not None:
                if result.get('status')!='PENDING':return result
                self.assertEqual(result.get('last_error'),'OPERATION_BUSY',result)
            await asyncio.sleep(.1)
        self.fail('La recuperación no terminó: '+repr(writer.queue.get(identifier,writer.identity)))

    async def test_api_only_enqueues_and_two_services_create_one_effect(self):
        identifier=self.prepare();first=self.enqueue(identifier)
        self.assertEqual(first,self.enqueue(identifier));self.assertEqual(self.effects(),0)
        self.assertEqual(first['write_job']['status'],'PENDING')
        one,two=self.writer(),self.writer()
        results=await asyncio.gather(one.run_once(),two.run_once())
        self.assertEqual(sum(result is not None for result in results),1)
        operation=one.ops.get(identifier,one.identity)
        self.assertEqual(operation['status'],'SUCCEEDED');self.assertEqual(self.effects(),1)
        self.assertEqual([e['actor_id'] for e in operation['events']],['reader-demo','service:write-worker','service:write-worker'])
        self.assertIsNone(await self.writer().run_once())

    async def test_revoked_requester_or_approver_before_send_produces_no_effect(self):
        identifier=self.prepare();self.enqueue(identifier)
        for actor,roles in (('reader-demo',frozenset()),('approver-demo',frozenset())):
            with self.subTest(actor=actor):
                original=self.directory.resolve(actor,'workspace-alpha').roles
                self.directory.set_membership(actor,'workspace-alpha',roles,enabled=False)
                writer=self.writer();result=await writer.run_once()
                self.assertEqual(result['result_status'],'NOT_APPLIED');self.assertEqual(self.effects(),0)
                self.assertEqual(writer.ops.get(identifier,writer.identity)['outcome_code'],'FORBIDDEN')
                self.directory.set_membership(actor,'workspace-alpha',original)
                if actor=='reader-demo':
                    # Intención nueva explícita, no reutilizar una operación NOT_APPLIED.
                    self.key=str(uuid4())
                    self.item=self.post('/runs/'+self.run['run_id']+'/proposals',{'request_key':self.key},expected=201)['proposal']
                    identifier=self.prepare();self.enqueue(identifier)

    async def test_final_gate_detects_revocation_after_ledger_claim_without_request(self):
        identifier=self.prepare();self.enqueue(identifier);writer=self.writer()
        original=Operations._transition
        def revoke(ops,db,row,status,identity,**kwargs):
            result=original(ops,db,row,status,identity,**kwargs)
            if ops.service_execution and status=='DISPATCHING':
                self.directory.set_membership('approver-demo','workspace-alpha',frozenset(),enabled=False)
            return result
        with patch.object(Operations,'_transition',revoke),patch.object(writer.ops.provider.client,'request',side_effect=AssertionError('must not send')) as request:
            result=await writer.run_once()
        request.assert_not_called()
        self.assertEqual(result['result_status'],'NOT_APPLIED');self.assertEqual(self.effects(),0)

    async def test_after_effect_revocation_and_lost_response_recover_by_lookup_only(self):
        identifier=self.prepare();self.enqueue(identifier,interrupt='after-send');first=self.writer()
        request=first.ops.provider.client.request
        def accepted_then_revoke(*args):
            receipt=request(*args)
            self.directory.set_membership('reader-demo','workspace-alpha',frozenset(),enabled=False)
            self.directory.set_membership('approver-demo','workspace-alpha',frozenset(),enabled=False)
            return receipt
        with patch.object(first.ops.provider.client,'request',side_effect=accepted_then_revoke) as sent:
            result=await first.run_once()
        self.assertEqual(result['status'],'PENDING');sent.assert_called_once();self.assertEqual(self.effects(),1)
        self.assertEqual(self.client.get('/api/operations/'+identifier,headers=self.headers()).status_code,401)
        await asyncio.to_thread(wait_for_expired_lease,self.runs,identifier)
        recovered=self.writer()
        with patch.object(recovered.ops.provider.client,'request',side_effect=AssertionError('must not resend')) as resend,patch.object(
                recovered.ops.provider.client,'lookup',wraps=recovered.ops.provider.client.lookup) as lookup:
            result=await self.recover(recovered,identifier)
        resend.assert_not_called();self.assertEqual(lookup.call_count,1,repr(result))
        self.assertEqual((result['status'],result['result_status']),('DONE','SUCCEEDED'))
        self.assertGreaterEqual(result['attempts'],2);self.assertLessEqual(result['attempts'],5)
        self.assertEqual(self.effects(),1)
        self.assertEqual([e['status'] for e in recovered.ops.get(identifier,recovered.identity)['events']],
            ['PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED'])

    async def test_session_revocation_does_not_cancel_admitted_intent(self):
        identifier=self.prepare();self.enqueue(identifier)
        self.directory.revoke(self.alpha['session_id']);self.directory.revoke(self.approver['session_id'])
        result=await self.writer().run_once()
        self.assertEqual(result['result_status'],'SUCCEEDED');self.assertEqual(self.effects(),1)

    async def test_scope_is_not_authority_to_prepare_send_unclaimed_or_read_other_owner(self):
        identifier=self.prepare();writer=self.writer()
        for action in (lambda:writer.ops.dispatch(identifier,writer.identity),lambda:writer.ops.reconcile(identifier,writer.identity)):
            with self.assertRaises(ContractError) as error:action()
            self.assertEqual(error.exception.code,'LEASE_LOST')
        with self.assertRaises(ContractError):writer.ops.prepare(self.item['proposal_id'],writer.identity)
        other=self.writer('reader-beta','workspace-beta')
        with self.assertRaises(ContractError) as error:other.ops.get(identifier,other.identity)
        self.assertEqual(error.exception.code,'NOT_FOUND')
        self.assertIsNone(await other.run_once());self.assertEqual(self.effects(),0)

    async def test_uncertain_absence_parks_without_resending(self):
        identifier=self.prepare();self.enqueue(identifier,interrupt='before-send')
        first=self.writer();self.assertEqual((await first.run_once())['status'],'PENDING')
        await asyncio.to_thread(wait_for_expired_lease,self.runs,identifier)
        second=self.writer()
        with patch.object(second.ops.provider.client,'request',side_effect=AssertionError('must not send')):
            result=await second.run_once()
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertEqual(second.ops.get(identifier,second.identity)['status'],'UNKNOWN')
        self.assertEqual(self.effects(),0);self.assertIsNone(await second.run_once())

    async def test_api_denies_enqueue_after_revocation_and_hides_jobs_cross_scope(self):
        identifier=self.prepare()
        self.directory.set_membership('approver-demo','workspace-alpha',frozenset({'reader'}))
        self.post('/operations/'+identifier+'/dispatch',{},expected=403)
        with self.runs._database() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM write_jobs').fetchone()['n'],0)
        self.directory.set_membership('approver-demo','workspace-alpha',frozenset({'approver'}))
        self.enqueue(identifier)
        self.post('/operations/'+identifier+'/dispatch',{},credential=self.beta,expected=404)
        self.post('/operations/'+identifier+'/dispatch',{},credential=self.approver,expected=403)

    async def test_expired_service_owner_cannot_send_after_new_fenced_claim(self):
        identifier=self.prepare();self.enqueue(identifier);writer=self.writer()
        old=writer.queue.claim('old',writer.identity,lease_seconds=2)
        for _ in range(100):
            with self.runs._database() as db:
                expired=db.execute('SELECT lease_until<=clock_timestamp() AS expired FROM write_jobs WHERE job_id=%s',
                    (old.job_id,)).fetchone()['expired']
            if expired:break
            await asyncio.sleep(.05)
        else:self.fail('lease did not expire')
        new=writer.queue.claim('new',writer.identity)
        self.assertGreater(new.fencing_token,old.fencing_token)
        stale=writer.ops.with_guard(lambda db:writer.queue.guard(db,old))
        with self.assertRaises(ContractError) as denied:stale.dispatch(identifier,writer.identity)
        self.assertEqual(denied.exception.code,'LEASE_LOST');self.assertEqual(self.effects(),0)
        active=writer.ops.with_guard(lambda db:writer.queue.guard(db,new))
        self.assertEqual(active.dispatch(identifier,writer.identity)['status'],'SUCCEEDED')
        writer.queue.finish(new,status='DONE',result='SUCCEEDED')
        self.assertEqual(self.effects(),1)

    async def test_service_process_death_after_effect_recovers_with_fresh_runtime(self):
        import subprocess,sys
        from pathlib import Path
        identifier=self.prepare();self.enqueue(identifier)
        program="""import asyncio,sys
from pathlib import Path
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.user_identity import UserIdentityStore
from harness.user_write_worker import create_worker
runs=PostgresRunStore(local_dsn(),schema=sys.argv[1])
worker=create_worker(runs,UserIdentityStore(runs),'reader-demo','workspace-alpha',Path(sys.argv[2]),
    lease_seconds=2,crash_at='after-send')
asyncio.run(worker.run_once())
"""
        root=Path(__file__).resolve().parents[1]
        dead=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',program,self.schema,str(self.path)],
            cwd=root,capture_output=True,text=True,timeout=20)
        self.assertEqual(dead.returncode,17,dead.stderr);self.assertEqual(self.effects(),1)
        self.directory.set_membership('reader-demo','workspace-alpha',frozenset(),enabled=False)
        await asyncio.to_thread(wait_for_expired_lease,self.runs,identifier)
        fresh=self.writer()
        with patch.object(fresh.ops.provider.client,'request',side_effect=AssertionError('must not resend')):
            result=await fresh.run_once()
        self.assertEqual(result['result_status'],'SUCCEEDED');self.assertEqual(self.effects(),1)
