"""Propuestas con dos credenciales, grants actuales y consumo único sin despacho."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import test_authenticated_runs as fixtures


class AuthenticatedProposalTests(unittest.IsolatedAsyncioTestCase):
    cleanup=fixtures.AuthenticatedRunTests.cleanup
    headers=fixtures.AuthenticatedRunTests.headers
    start=fixtures.AuthenticatedRunTests.start
    worker=fixtures.AuthenticatedRunTests.worker

    async def asyncSetUp(self):
        await fixtures.AuthenticatedRunTests.asyncSetUp(self)
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'provider.sqlite3'
        self.client.app.state.user_proposals.provider_path=self.path
        self.run=self.start(scenario='stale')
        self.assertEqual((await self.worker().run_once())['status'],'COMPLETED')
        self.key=str(uuid4())
        self.item=self.post('/runs/'+self.run['run_id']+'/proposals',{'request_key':self.key},expected=201)['proposal']

    def post(self,path,body,credential=None,expected=200):
        response=self.client.post('/api'+path,headers=self.headers(credential),json=body)
        self.assertEqual(response.status_code,expected,response.text)
        return response.json()

    def decide(self,credential=None,expected=200,**overrides):
        body={'payload_hash':self.item['payload_hash'],'expected_version':0,'decision':'APPROVED',**overrides}
        return self.post('/proposals/'+self.item['proposal_id']+'/decision',body,credential or self.approver,expected)

    def get(self,credential=None):
        return self.client.get('/api/proposals/'+self.item['proposal_id'],headers=self.headers(credential))

    async def test_two_credentials_approve_exact_proposal_then_prepare_unique_operation(self):
        reviewer=self.get(self.approver).json()['proposal']
        self.assertEqual(reviewer['payload_hash'],self.item['payload_hash'])
        self.assertEqual(reviewer['payload']['context']['run_id'],self.run['run_id'])
        first=self.decide();self.assertEqual(first['proposal']['decided_by'],'approver-demo')
        self.assertEqual(self.decide(),first)
        prepared=self.post('/proposals/'+self.item['proposal_id']+'/operation',{})
        again=self.post('/proposals/'+self.item['proposal_id']+'/operation',{})
        self.assertEqual(again,prepared)
        self.assertEqual((prepared['proposal']['status'],prepared['operation']['status']),('CONSUMED','PREPARED'))
        oid=prepared['operation']['operation_id']
        self.assertEqual(self.client.get('/api/operations/'+oid,headers=self.headers()).json()['events'][0]['actor_id'],'reader-demo')
        with self.runs._database() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM operations').fetchone()['n'],1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM write_jobs').fetchone()['n'],0)
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
        after=self.client.get('/api/runs/'+self.run['run_id'],headers=self.headers()).json()
        self.assertEqual((after['model_calls'],after['tool_calls']),(4,3))

    async def test_self_approval_is_denied_even_with_both_roles_and_forged_headers(self):
        self.directory.set_membership('reader-demo','workspace-alpha',frozenset({'reader','operator','approver'}))
        self.decide(credential=self.alpha,expected=403)
        headers=self.headers();headers['X-Actor-ID']='approver-demo'
        body={'payload_hash':self.item['payload_hash'],'expected_version':0,'decision':'APPROVED'}
        self.assertEqual(self.client.post('/api/proposals/'+self.item['proposal_id']+'/decision',headers=headers,json=body).status_code,403)
        self.assertEqual(self.get().json()['proposal']['status'],'PENDING_APPROVAL')

    async def test_reader_same_workspace_and_other_workspace_cannot_review_or_approve(self):
        self.assertEqual(self.get(self.beta).status_code,404)
        self.decide(credential=self.beta,expected=404)
        self.assertEqual(self.client.get('/api/proposals',headers=self.headers(self.beta)).json()['items'],[])
        with self.runs._database() as db:
            db.execute("INSERT INTO user_accounts(actor_id) VALUES ('reader-other')")
            db.execute("""INSERT INTO user_memberships(actor_id,workspace_id,roles,projects,repositories,jobs)
                SELECT 'reader-other',workspace_id,roles,projects,repositories,jobs FROM user_memberships WHERE actor_id='reader-demo'""")
        other=self.directory.issue('reader-other','workspace-alpha')
        self.assertEqual(self.get(other).status_code,404)
        self.decide(credential=other,expected=404)
        self.assertEqual(len(self.client.get('/api/proposals',headers=self.headers(self.approver)).json()['items']),1)
        self.assertEqual(self.client.get('/api/runs/'+self.run['run_id'],headers=self.headers(self.approver)).status_code,403)

    async def test_grants_revoked_before_decision_and_before_consumption_are_rechecked(self):
        self.directory.set_membership('approver-demo','workspace-alpha',frozenset({'reader'}))
        self.decide(expected=403)
        self.directory.set_membership('approver-demo','workspace-alpha',frozenset({'approver'}))
        self.decide()
        self.directory.set_membership('approver-demo','workspace-alpha',frozenset({'reader'}))
        self.post('/proposals/'+self.item['proposal_id']+'/operation',{},expected=403)
        self.assertEqual(self.get().json()['proposal']['status'],'APPROVED')
        self.assertFalse(self.path.exists())  # No consulta MCP si falta autoridad actual.
        with self.runs._database() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM operations').fetchone()['n'],0)

    async def test_requester_revocation_prevents_new_approval_but_rejection_can_close_it(self):
        self.directory.set_membership('reader-demo','workspace-alpha',frozenset({'reader'}))
        self.decide(expected=403)
        rejected=self.decide(decision='REJECTED')
        self.assertEqual(rejected['proposal']['status'],'REJECTED')
        self.assertEqual(rejected['proposal']['decided_by'],'approver-demo')

    async def test_tampered_hash_version_actor_and_client_key_are_not_silently_accepted(self):
        self.decide(payload_hash='0'*64,expected=409)
        self.decide(expected_version=1,expected=409)
        self.decide(actor_id='approver-demo',expected=422)
        same=self.post('/runs/'+self.run['run_id']+'/proposals',{'request_key':self.key},expected=201)
        self.assertEqual(same['proposal'],self.item)
        self.decide()
        self.decide(decision='REJECTED',expected=409)
        self.post('/proposals/'+self.item['proposal_id']+'/operation',{},credential=self.approver,expected=403)

    async def test_directory_failure_during_live_revalidation_does_not_consume(self):
        self.decide()
        with patch.object(self.directory,'resolve',side_effect=RuntimeError('PRIVATE_SENTINEL')):
            result=self.post('/proposals/'+self.item['proposal_id']+'/operation',{},expected=503)
        self.assertEqual(result,{'error':'IDENTITY_UNAVAILABLE'})
        self.assertEqual(self.get().json()['proposal']['status'],'APPROVED')
        self.assertFalse(self.path.exists())
