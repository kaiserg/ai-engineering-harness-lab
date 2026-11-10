"""Cierre MCP, revisión autenticada y ledger/job atómicos sin reenvío."""
import asyncio
from datetime import datetime,timezone,timedelta
import unittest
from unittest.mock import patch
import test_user_write_worker as fixtures
from harness.authenticated_resolutions import AuthenticatedResolutions
from harness.contracts import ContractError


class AuthenticatedResolutionTests(unittest.IsolatedAsyncioTestCase):
    cleanup=fixtures.UserWriteWorkerTests.cleanup
    headers=fixtures.UserWriteWorkerTests.headers
    start=fixtures.UserWriteWorkerTests.start
    worker=fixtures.UserWriteWorkerTests.worker
    writer=fixtures.UserWriteWorkerTests.writer
    post=fixtures.UserWriteWorkerTests.post
    decide=fixtures.UserWriteWorkerTests.decide
    get=fixtures.UserWriteWorkerTests.get
    prepare=fixtures.UserWriteWorkerTests.prepare
    enqueue=fixtures.UserWriteWorkerTests.enqueue
    effects=fixtures.UserWriteWorkerTests.effects
    asyncSetUp=fixtures.UserWriteWorkerTests.asyncSetUp

    async def unknown(self,accepted=False):
        identifier=self.prepare();self.enqueue(identifier);writer=self.writer()
        original=writer.ops.provider.client.request
        def lost(*args):
            if accepted:original(*args)
            raise TimeoutError('PRIVATE_SENTINEL')
        with patch.object(writer.ops.provider.client,'request',side_effect=lost),patch.object(writer.ops.provider.client,'lookup',return_value=None):
            result=await writer.run_once()
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertEqual(writer.ops.get(identifier,writer.identity)['status'],'UNKNOWN')
        return identifier

    def review(self,identifier):return self.post('/operations/'+identifier+'/resolution',{})['resolution']

    def confirm(self,identifier,review,credential=None,expected=200,**overrides):
        return self.post('/operations/'+identifier+'/resolution/confirm',
            {'payload_hash':review['payload_hash'],'expected_version':review['version'],**overrides},
            credential or self.approver,expected)

    async def test_closure_blocks_late_request_and_confirmation_finishes_job_atomically(self):
        identifier=await self.unknown();review=self.review(identifier)
        self.assertEqual(review,self.review(identifier));self.assertEqual(review['status'],'PENDING_REVIEW')
        self.assertEqual(review['payload']['conclusion'],'NOT_APPLIED')
        writer=self.writer()
        with self.assertRaises(ContractError) as denied:
            writer.ops.provider.client.request(writer.ops.provider.client.identity,identifier,self.item['payload_hash'],
                self.item['payload']['arguments'],datetime.now(timezone.utc)+timedelta(seconds=30))
        self.assertEqual(denied.exception.code,'OPERATION_CLOSED');self.assertEqual(self.effects(),0)
        result=self.confirm(identifier,review)
        self.assertEqual(result['resolution']['reviewed_by'],'approver-demo')
        self.assertEqual(self.confirm(identifier,review),result)
        operation=writer.ops.get(identifier,writer.identity);job=writer.queue.get(identifier,writer.identity)
        self.assertEqual((operation['status'],job['status'],job['result_status']),('NOT_APPLIED','DONE','NOT_APPLIED'))
        self.assertEqual(operation['events'][-1]['actor_id'],'approver-demo')
        self.assertEqual(self.effects(),0);self.assertIsNone(await writer.run_once())
        self.assertEqual(self.get().json()['resolution']['status'],'CONFIRMED')

    async def test_same_actor_is_not_reviewer_even_with_both_roles_and_wrong_scope_is_hidden(self):
        identifier=await self.unknown();review=self.review(identifier)
        self.directory.set_membership('reader-demo','workspace-alpha',frozenset({'reader','operator','approver'}))
        self.confirm(identifier,review,credential=self.alpha,expected=403)
        self.confirm(identifier,review,credential=self.beta,expected=404)
        self.post('/operations/'+identifier+'/resolution',{},credential=self.approver,expected=403)
        self.confirm(identifier,review,payload_hash='0'*64,expected=409)
        self.confirm(identifier,review,expected_version=1,expected=409)
        self.confirm(identifier,review,conclusion='NOT_APPLIED',expected=422)
        self.assertEqual(self.writer().ops.get(identifier,self.writer().identity)['status'],'UNKNOWN')

    async def test_disabled_requester_does_not_block_authorized_reviewer_from_confirming(self):
        identifier=await self.unknown();review=self.review(identifier)
        self.directory.set_membership('reader-demo','workspace-alpha',frozenset(),enabled=False)
        self.directory.revoke(self.alpha['session_id'])
        result=self.confirm(identifier,review)
        self.assertEqual(result['resolution']['status'],'CONFIRMED')
        writer=self.writer();self.assertEqual(writer.queue.get(identifier,writer.identity)['status'],'DONE')

    async def test_revoked_reviewer_cannot_confirm_and_does_not_change_pending_evidence(self):
        identifier=await self.unknown();review=self.review(identifier)
        self.directory.set_membership('approver-demo','workspace-alpha',frozenset({'reader'}))
        self.confirm(identifier,review,expected=403)
        self.assertEqual(self.get().json()['resolution']['status'],'PENDING_REVIEW')
        self.assertEqual(self.writer().ops.get(identifier,self.writer().identity)['status'],'UNKNOWN')

    async def test_accepted_receipt_can_be_confirmed_without_a_second_send(self):
        identifier=await self.unknown(accepted=True);review=self.review(identifier)
        self.assertEqual(review['payload']['conclusion'],'SUCCEEDED')
        with patch('harness.adapters.mcp_jenkins_write.MCPJenkinsWrite.request',side_effect=AssertionError('must not resend')):
            self.confirm(identifier,review)
        writer=self.writer();self.assertEqual(writer.ops.get(identifier,writer.identity)['status'],'SUCCEEDED')
        self.assertEqual(writer.queue.get(identifier,writer.identity)['status'],'DONE');self.assertEqual(self.effects(),1)

    async def test_callback_failure_rolls_back_resolution_ledger_and_job_together(self):
        identifier=await self.unknown();review=self.review(identifier)
        service=AuthenticatedResolutions(self.client.app.state.user_proposals)
        reviewer=self.directory.resolve('approver-demo','workspace-alpha')
        resolutions,scope=service.context(identifier,reviewer,action='confirm')
        def failed(*args):raise ContractError('CONFLICT')
        resolutions.on_confirm=failed
        with self.assertRaises(ContractError):resolutions.confirm(identifier,scope,reviewer,payload_hash=review['payload_hash'],expected_version=0)
        writer=self.writer();self.assertEqual(writer.ops.get(identifier,writer.identity)['status'],'UNKNOWN')
        self.assertEqual(writer.queue.get(identifier,writer.identity)['status'],'NEEDS_REVIEW')
        self.assertEqual(self.get().json()['resolution']['status'],'PENDING_REVIEW')
        self.confirm(identifier,review)

    async def test_concurrent_duplicate_confirmations_have_one_transition_and_one_job_finish(self):
        identifier=await self.unknown();review=self.review(identifier)
        values=await asyncio.gather(asyncio.to_thread(self.confirm,identifier,review),asyncio.to_thread(self.confirm,identifier,review))
        self.assertEqual(values[0],values[1])
        writer=self.writer();self.assertEqual(len(writer.ops.get(identifier,writer.identity)['events']),4)
        self.assertEqual(writer.queue.get(identifier,writer.identity)['status'],'DONE')

    async def test_running_or_pending_job_cannot_be_taken_over_by_manual_review(self):
        identifier=self.prepare();self.enqueue(identifier)
        self.post('/operations/'+identifier+'/resolution',{},expected=409)
        writer=self.writer();claim=writer.queue.claim('running',writer.identity)
        self.post('/operations/'+identifier+'/resolution',{},expected=409)
        self.assertEqual(writer.queue.get(identifier,writer.identity)['status'],'RUNNING')
        self.assertEqual(self.effects(),0)

    async def test_reviewer_revoked_while_checking_remote_proof_cannot_commit_confirmation(self):
        from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite
        identifier=await self.unknown();review=self.review(identifier)
        original=MCPJenkinsWrite.close_request
        def proof_then_revoke(client,*args):
            proof=original(client,*args)
            self.directory.set_membership('approver-demo','workspace-alpha',frozenset({'reader'}))
            return proof
        with patch.object(MCPJenkinsWrite,'close_request',proof_then_revoke):
            self.confirm(identifier,review,expected=403)
        writer=self.writer()
        self.assertEqual(writer.ops.get(identifier,writer.identity)['status'],'UNKNOWN')
        self.assertEqual(writer.queue.get(identifier,writer.identity)['status'],'NEEDS_REVIEW')
        self.assertEqual(self.get().json()['resolution']['status'],'PENDING_REVIEW')

    async def test_exhausted_claims_can_investigate_expired_dispatching_without_resetting_attempts(self):
        from lease_wait import wait_for_expired_lease
        identifier=self.prepare();self.enqueue(identifier,interrupt='before-send');writer=self.writer()
        self.assertEqual((await writer.run_once())['status'],'PENDING')
        await asyncio.to_thread(wait_for_expired_lease,self.runs,identifier)
        for _ in range(4):
            claim=writer.queue.claim('test',writer.identity)
            writer.queue.finish(claim,status='PENDING')
        self.assertIsNone(writer.queue.claim('exhausted',writer.identity))
        self.assertEqual(writer.ops.get(identifier,writer.identity)['status'],'DISPATCHING')
        review=self.review(identifier)
        self.assertEqual(writer.ops.get(identifier,writer.identity)['status'],'UNKNOWN')
        self.assertEqual(review['payload']['conclusion'],'NOT_APPLIED')
        self.confirm(identifier,review)
        job=writer.queue.get(identifier,writer.identity)
        self.assertEqual((job['status'],job['attempts'],job['result_status']),('DONE',5,'NOT_APPLIED'))
        self.assertEqual(self.effects(),0)
