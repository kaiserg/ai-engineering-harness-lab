"""Ledger PG y proveedor SQLite independiente: ningún efecto debe repetirse a ciegas."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from lease_wait import wait_for_expired_lease
from unittest.mock import patch
import test_proposals as fixtures
from harness.contracts import ContractError
from harness.jenkins_simulator import JenkinsSimulator
from harness.operations import Operations,OperationInterrupted
from harness.proposals import ProposalStore


class OperationTests(unittest.IsolatedAsyncioTestCase):
    code=fixtures.ProposalTests.code
    decide=fixtures.ProposalTests.decide
    asyncTearDown=fixtures.ProposalTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.ProposalTests.asyncSetUp(self)
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.provider=JenkinsSimulator(Path(self.directory.name)/'provider.sqlite3')
        self.principals={self.operator.actor_id:self.operator,self.approver.actor_id:self.approver}
        self.ops=Operations(self.store,self.provider,self.principals)

    def approved(self):
        self.decide()
        return self.ops.prepare(self.item['proposal_id'],self.operator)

    def effects(self):
        with self.provider._db() as db:return db.execute('SELECT count(*) AS n FROM requests').fetchone()['n']

    async def test_consume_and_ledger_are_atomic_and_repeatable(self):
        row=self.approved()
        self.assertEqual(self.ops.prepare(self.item['proposal_id'],self.operator),row)
        self.assertEqual(self.store.get(self.item['proposal_id'],self.operator)['status'],'CONSUMED')
        self.assertEqual(self.effects(),0)
        self.assertEqual(self.decide()['status'],'CONSUMED')
        self.assertEqual(self.runs.load(self.run.run_id,self.operator),self.run)
        self.assertEqual(self.ops.get(row['operation_id'],self.operator)['events'][0]['status'],'PREPARED')

    async def test_unapproved_or_rejected_cannot_create_operation(self):
        self.code('CONFLICT',lambda:self.ops.prepare(self.item['proposal_id'],self.operator))
        self.decide(decision='REJECTED')
        self.code('CONFLICT',lambda:self.ops.prepare(self.item['proposal_id'],self.operator))
        self.assertEqual(self.effects(),0)

    async def test_revision_changed_before_consumption_invalidates_proposal(self):
        self.decide();self.provider.set_revision(self.operator.workspace_id,'revision-B')
        self.code('STALE_PRECONDITION',lambda:self.ops.prepare(self.item['proposal_id'],self.operator))
        self.assertEqual(self.store.get(self.item['proposal_id'],self.operator)['status'],'INVALIDATED')
        with self.runs._database() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM operations').fetchone()['n'],0)

    async def test_revision_changed_before_provider_accepts_has_no_effect(self):
        row=self.approved();self.provider.set_revision(self.operator.workspace_id,'revision-B')
        done=self.ops.dispatch(row['operation_id'],self.operator)
        self.assertEqual((done['status'],done['outcome_code']),('NOT_APPLIED','STALE_PRECONDITION'))
        self.assertEqual(self.effects(),0)

    async def test_revoked_approver_prevents_dispatch(self):
        row=self.approved();revoked=replace(self.approver,jobs=frozenset())
        service=Operations(self.store,self.provider,{**self.principals,self.approver.actor_id:revoked})
        done=service.dispatch(row['operation_id'],self.operator)
        self.assertEqual((done['status'],done['outcome_code']),('NOT_APPLIED','FORBIDDEN'))
        self.assertEqual(self.effects(),0)

    async def test_dispatch_repeat_restart_and_concurrent_workers_produce_one_effect(self):
        row=self.approved();identifier=row['operation_id']
        def dispatch():
            try:return self.ops.dispatch(identifier,self.operator)['status']
            except ContractError as error:return error.code
        values=await asyncio.gather(asyncio.to_thread(dispatch),asyncio.to_thread(dispatch))
        self.assertIn('SUCCEEDED',values)
        self.assertTrue(set(values)<= {'SUCCEEDED','UNSAFE_RECOVERY'})
        reopened=Operations(ProposalStore(self.runs),JenkinsSimulator(self.provider.path),self.principals)
        self.assertEqual(reopened.dispatch(identifier,self.operator)['status'],'SUCCEEDED')
        self.assertEqual(self.effects(),1)
        self.assertEqual([e['status'] for e in reopened.get(identifier,self.operator)['events']],['PREPARED','DISPATCHING','SUCCEEDED'])

    async def test_response_lost_after_effect_is_reconciled_by_lookup_only(self):
        row=self.approved();request=self.provider.request
        def lost(*args):
            request(*args)
            raise TimeoutError('PRIVATE ERROR')
        with patch.object(self.provider,'request',side_effect=lost):
            unknown=self.ops.dispatch(row['operation_id'],self.operator)
        self.assertEqual(unknown['status'],'UNKNOWN');self.assertEqual(self.effects(),1)
        self.code('UNSAFE_RECOVERY',lambda:self.ops.dispatch(row['operation_id'],self.operator))
        with patch.object(self.provider,'request',side_effect=AssertionError('must not send')):
            result=self.ops.reconcile(row['operation_id'],self.operator)
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(self.effects(),1)
        self.assertNotIn('PRIVATE',json.dumps(self.ops.get(row['operation_id'],self.operator)))
        self.code('CONFLICT',lambda:self.ops._finish(row['operation_id'],self.operator,1,'SUCCEEDED',receipt='old'))

    async def test_interruption_before_send_requires_expired_lease_and_never_resends(self):
        row=self.approved()
        with self.assertRaises(OperationInterrupted):self.ops.dispatch(row['operation_id'],self.operator,interrupt='before-send')
        self.code('OPERATION_BUSY',lambda:self.ops.reconcile(row['operation_id'],self.operator))
        await asyncio.to_thread(wait_for_expired_lease,self.runs,row['operation_id'])
        result=self.ops.reconcile(row['operation_id'],self.operator)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(self.effects(),0)
        self.code('UNSAFE_RECOVERY',lambda:self.ops.dispatch(row['operation_id'],self.operator))
        self.assertEqual(self.ops.reconcile(row['operation_id'],self.operator)['status'],'UNKNOWN')

    async def test_provider_deduplicates_same_operation_and_rejects_changed_payload(self):
        row=self.approved();args=self.item['payload']['arguments'];identifier=row['operation_id']
        from datetime import datetime,timezone,timedelta
        cutoff=datetime.now(timezone.utc)+timedelta(seconds=10)
        first=self.provider.request(self.operator,identifier,self.item['payload_hash'],args,cutoff)
        self.assertEqual(self.provider.request(self.operator,identifier,self.item['payload_hash'],args,cutoff),first)
        self.code('CONFLICT',lambda:self.provider.request(self.operator,identifier,'other',args,cutoff))
        self.assertEqual(self.effects(),1)

    async def test_operation_scope_and_sql_immutability(self):
        row=self.approved();identifier=row['operation_id']
        other=replace(self.operator,workspace_id='other')
        service=Operations(self.store,self.provider,{**self.principals,other.actor_id:other})
        self.code('NOT_FOUND',lambda:service.get(identifier,other))
        with self.assertRaises(ContractError):
            with self.runs._database() as db:
                db.execute("UPDATE operations SET status='SUCCEEDED',version=version+1 WHERE operation_id=%s",(identifier,))
        self.assertEqual(self.ops.get(identifier,self.operator)['status'],'PREPARED')

    async def test_expired_approval_cannot_be_consumed(self):
        from hashlib import sha256
        from uuid import uuid4
        from psycopg import sql
        self.decide();identifier=str(uuid4())
        with self.runs._database() as db:
            row=db.execute('SELECT * FROM proposals WHERE proposal_id=%s',(self.item['proposal_id'],)).fetchone()
            past=db.execute("SELECT clock_timestamp()-interval '1 second' AS now").fetchone()['now']
            payload=json.loads(row['canonical_payload']);payload['expires_at']=past.isoformat()
            canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            row.update(proposal_id=identifier,request_key=str(uuid4()),expires_at=past,
                canonical_payload=canonical,payload_hash=sha256(canonical.encode()).hexdigest())
            db.execute(sql.SQL('INSERT INTO proposals ({}) VALUES ({})').format(sql.SQL(',').join(map(sql.Identifier,row)),
                sql.SQL(',').join(sql.Placeholder() for _ in row)),list(row.values()))
        self.code('APPROVAL_EXPIRED',lambda:self.ops.prepare(identifier,self.operator))
        self.assertEqual(self.effects(),0)

    async def test_unknown_operation_blocks_another_write_to_same_resource(self):
        first=self.approved()
        with patch.object(self.provider,'request',side_effect=TimeoutError):
            self.ops.dispatch(first['operation_id'],self.operator)
        from uuid import uuid4
        second=self.store.create(self.run.run_id,self.operator,str(uuid4()))
        self.store.decide(second['proposal_id'],self.approver,payload_hash=second['payload_hash'],expected_version=0,decision='APPROVED')
        prepared=self.ops.prepare(second['proposal_id'],self.operator)
        self.code('OPERATION_BUSY',lambda:self.ops.dispatch(prepared['operation_id'],self.operator))
        self.assertEqual(self.effects(),0)

    async def test_provider_rejects_send_after_lease_cutoff_without_effect(self):
        from datetime import datetime,timezone,timedelta
        row=self.approved()
        self.code('DISPATCH_EXPIRED',lambda:self.provider.request(self.operator,row['operation_id'],
            self.item['payload_hash'],self.item['payload']['arguments'],datetime.now(timezone.utc)-timedelta(seconds=1)))
        self.assertEqual(self.effects(),0)
