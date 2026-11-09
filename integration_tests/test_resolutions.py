"""UNKNOWN se resuelve con cierre durable y revisión, nunca con ausencia o texto libre."""
import asyncio
from datetime import datetime,timezone,timedelta
import unittest
from unittest.mock import patch
import test_operations as fixtures
from harness.contracts import ContractError
from harness.resolutions import Resolutions
from harness.operations import Operations
from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    code=fixtures.OperationTests.code
    decide=fixtures.OperationTests.decide
    approved=fixtures.OperationTests.approved
    effects=fixtures.OperationTests.effects
    asyncTearDown=fixtures.OperationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.OperationTests.asyncSetUp(self)
        self.resolutions=Resolutions(self.ops)

    def unknown(self,accepted=False):
        op=self.approved();request=self.provider.request
        def lost(*args):
            if accepted:request(*args)
            raise TimeoutError('PRIVATE TRANSPORT DETAIL')
        with patch.object(self.provider,'request',side_effect=lost):
            result=self.ops.dispatch(op['operation_id'],self.operator)
        self.assertEqual(result['status'],'UNKNOWN')
        return result['operation_id']

    def confirm(self,identifier,review,**overrides):
        kwargs={'payload_hash':review['payload_hash'],'expected_version':0};kwargs.update(overrides)
        return self.resolutions.confirm(identifier,self.operator,self.approver,**kwargs)

    async def test_closure_is_stable_and_fences_a_late_request_before_review(self):
        identifier=self.unknown();review=self.resolutions.prepare(identifier,self.operator)
        self.assertEqual(review,self.resolutions.prepare(identifier,self.operator))
        self.assertEqual(review,Resolutions(self.ops).get(identifier,self.operator))
        self.assertEqual(self.ops.get(identifier,self.operator)['status'],'UNKNOWN')
        self.code('OPERATION_CLOSED',lambda:self.provider.request(self.operator,identifier,self.item['payload_hash'],
            self.item['payload']['arguments'],datetime.now(timezone.utc)+timedelta(seconds=30)))
        self.assertEqual(self.effects(),0)

    async def test_confirm_records_actor_and_events_and_never_reenables_old_operation(self):
        identifier=self.unknown();review=self.resolutions.prepare(identifier,self.operator)
        result=self.confirm(identifier,review)
        self.assertEqual((result['status'],result['reviewed_by']),('CONFIRMED','approver-demo'))
        self.assertEqual(self.confirm(identifier,review),result)
        operation=self.ops.get(identifier,self.operator)
        self.assertEqual((operation['status'],operation['outcome_code']),('NOT_APPLIED','MANUAL_PROVIDER_CLOSURE'))
        self.assertEqual(operation['events'][-1]['actor_id'],'approver-demo')
        self.assertEqual(len(operation['events']),4)
        self.assertEqual(self.ops.dispatch(identifier,self.operator)['status'],'NOT_APPLIED')
        self.assertEqual(self.effects(),0)
        self.assertEqual(self.store.get(self.item['proposal_id'],self.operator)['status'],'CONSUMED')

    async def test_self_review_wrong_hash_version_and_workspace_are_rejected(self):
        from dataclasses import replace
        identifier=self.unknown();review=self.resolutions.prepare(identifier,self.operator)
        self.code('FORBIDDEN',lambda:self.resolutions.confirm(identifier,self.operator,self.operator,
            payload_hash=review['payload_hash'],expected_version=0))
        self.code('CONFLICT',lambda:self.confirm(identifier,review,payload_hash='0'*64))
        self.code('CONFLICT',lambda:self.confirm(identifier,review,expected_version=1))
        self.code('INVALID_ARGUMENT',lambda:self.confirm(identifier,review,expected_version=True))
        self.code('FORBIDDEN',lambda:self.resolutions.confirm(identifier,self.operator,replace(self.approver,workspace_id='other'),
            payload_hash=review['payload_hash'],expected_version=0))
        self.assertEqual(self.ops.get(identifier,self.operator)['status'],'UNKNOWN')

    async def test_sql_cannot_claim_no_effect_without_confirmed_resolution(self):
        identifier=self.unknown();self.resolutions.prepare(identifier,self.operator)
        with self.assertRaises(ContractError):
            with self.runs._database() as db:
                db.execute("UPDATE operations SET status='NOT_APPLIED',version=version+1 WHERE operation_id=%s",(identifier,))
        self.assertEqual(self.ops.get(identifier,self.operator)['status'],'UNKNOWN')

    async def test_automatic_resolution_wins_without_stale_manual_overwrite(self):
        identifier=self.unknown(accepted=True)
        review=self.resolutions.prepare(identifier,self.operator)
        self.assertEqual(review['payload']['conclusion'],'SUCCEEDED')
        self.assertEqual(self.ops.reconcile(identifier,self.operator)['status'],'SUCCEEDED')
        self.code('CONFLICT',lambda:self.confirm(identifier,review))
        self.assertEqual(self.resolutions.get(identifier,self.operator)['effective_status'],'STALE')
        self.assertEqual(self.effects(),1)

    async def test_changed_or_invalid_provider_evidence_keeps_unknown(self):
        identifier=self.unknown();proof=self.provider.close_request(self.operator,identifier,self.item['payload_hash'])
        with patch.object(self.provider,'close_request',return_value=proof|{'closed':False}):
            self.code('INVALID_TOOL_OUTPUT',lambda:self.resolutions.prepare(identifier,self.operator))
        self.assertIsNone(self.resolutions.get(identifier,self.operator))
        review=self.resolutions.prepare(identifier,self.operator)
        with patch.object(self.provider,'close_request',return_value=proof|{'closed_at':'2000-01-01T00:00:00+00:00'}):
            self.code('CONFLICT',lambda:self.confirm(identifier,review))
        self.assertEqual(self.ops.get(identifier,self.operator)['status'],'UNKNOWN')
        self.assertEqual(self.resolutions.get(identifier,self.operator)['status'],'PENDING_REVIEW')

    async def test_concurrent_review_retries_record_only_one_transition(self):
        identifier=self.unknown();review=self.resolutions.prepare(identifier,self.operator)
        values=await asyncio.gather(asyncio.to_thread(self.confirm,identifier,review),asyncio.to_thread(self.confirm,identifier,review))
        self.assertEqual(values[0],values[1]);self.assertEqual(len(self.ops.get(identifier,self.operator)['events']),4)

    async def test_real_mcp_closure_and_review_preserve_zero_effects(self):
        identifier=self.unknown()
        service=Operations(self.store,MCPJenkinsWrite(self.operator,self.provider.path),self.principals)
        resolutions=Resolutions(service)
        review=resolutions.prepare(identifier,self.operator)
        resolutions.confirm(identifier,self.operator,self.approver,payload_hash=review['payload_hash'],expected_version=0)
        self.assertEqual(service.get(identifier,self.operator)['status'],'NOT_APPLIED')
        self.assertEqual(self.effects(),0)

    async def test_review_can_confirm_an_accepted_effect_without_resending(self):
        identifier=self.unknown(accepted=True);review=self.resolutions.prepare(identifier,self.operator)
        with patch.object(self.provider,'request',side_effect=AssertionError('must not resend')):
            self.confirm(identifier,review)
        row=self.ops.get(identifier,self.operator)
        self.assertEqual(row['status'],'SUCCEEDED')
        self.assertEqual(row['receipt'],'queue:'+identifier)
        self.assertEqual(row['events'][-1]['actor_id'],'approver-demo')
        self.assertEqual(self.effects(),1)
