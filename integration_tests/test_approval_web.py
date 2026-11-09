"""API M4 opt-in, PostgreSQL aislado y MCP real; sin identidades aportadas por HTTP."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from lease_wait import wait_for_expired_lease
from uuid import uuid4
from fastapi.testclient import TestClient
from harness.postgres_config import local_dsn
from harness.web import create_app
import test_proposals as fixtures

HEADERS={'X-Harness-Client':'local-ui'}


class ApprovalWebTests(unittest.TestCase):
    def setUp(self):
        asyncio.run(fixtures.ProposalTests.asyncSetUp(self))
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'provider.sqlite3'

    def tearDown(self):asyncio.run(fixtures.ProposalTests.asyncTearDown(self))

    def app(self,enabled=True):
        return create_app(storage='postgres',pg_dsn=local_dsn(),pg_schema=self.schema,
            approvals_demo=enabled,approval_provider_path=self.path)

    def post(self,client,path,body):
        response=client.post('/api'+path,json=body,headers=HEADERS)
        self.assertLess(response.status_code,300,response.text)
        return response.json()

    def proposal(self,client):
        return self.post(client,'/runs/'+self.run.run_id+'/proposals',{'request_key':str(uuid4())})

    def approve(self,client,item):
        row=item['proposal']
        return self.post(client,'/proposals/'+row['proposal_id']+'/decision',{
            'payload_hash':row['payload_hash'],'expected_version':0,'decision':'APPROVED'})

    def test_opt_in_hides_routes_and_rejects_other_backends(self):
        with TestClient(self.app(False),base_url='http://127.0.0.1') as client:
            self.assertFalse(client.get('/api/config').json()['approvals_demo'])
            self.assertEqual(client.get('/api/runs/'+self.run.run_id+'/proposals').status_code,404)
        with self.assertRaises(ValueError):create_app(approvals_demo=True)
        from dataclasses import replace
        from harness.composition import demo_identity
        with self.assertRaises(ValueError):
            create_app(storage='postgres',approvals_demo=True,identity=replace(demo_identity(),actor_id='other'))

    def test_full_api_flow_retry_and_restart_preserve_ledger_and_one_effect(self):
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            key=str(uuid4());path='/runs/'+self.run.run_id+'/proposals'
            first=self.post(client,path,{'request_key':key})
            self.assertEqual(self.post(client,path,{'request_key':key}),first)
            approved=self.approve(client,first)
            self.assertEqual(approved['proposal']['decided_by'],'approver-demo')
            identifier=first['proposal']['proposal_id']
            prepared=self.post(client,'/proposals/'+identifier+'/operation',{})
            op=prepared['operation']['operation_id']
            accepted=self.post(client,'/operations/'+op+'/dispatch',{})
            self.assertEqual(accepted['status'],'SUCCEEDED')
            self.assertEqual(self.post(client,'/operations/'+op+'/dispatch',{}),accepted)
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            loaded=client.get('/api/proposals/'+identifier).json()
            self.assertEqual(loaded['operation']['status'],'SUCCEEDED')
            self.assertEqual(len(loaded['operation']['events']),3)
            self.assertGreaterEqual(len(client.get('/api/runs/'+self.run.run_id+'/proposals').json()['items']),1)
        from harness.jenkins_simulator import JenkinsSimulator
        with JenkinsSimulator(self.path)._db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM requests').fetchone()['n'],1)
        self.assertEqual(self.runs.load(self.run.run_id,self.operator),self.run)

    def test_identity_payload_roles_hash_and_browser_origin_cannot_bypass_guards(self):
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            first=self.proposal(client);identifier=first['proposal']['proposal_id']
            decision={'payload_hash':first['proposal']['payload_hash'],'expected_version':0,'decision':'APPROVED'}
            for extra in ({'actor_id':'admin'},{'roles':['approver']},{'workspace_id':'other'}):
                response=client.post('/api/proposals/'+identifier+'/decision',json=decision|extra,headers=HEADERS)
                self.assertEqual(response.status_code,422)
            self.assertEqual(client.post('/api/proposals/'+identifier+'/decision',json=decision,headers=HEADERS|{'Origin':'https://other.example'}).status_code,403)
            self.assertEqual(client.post('/api/proposals/'+identifier+'/decision',json=decision|{'payload_hash':'0'*64},headers=HEADERS).status_code,409)
            self.assertEqual(client.post('/api/proposals/'+identifier+'/operation',json={},headers=HEADERS).status_code,409)
            self.assertEqual(client.post('/api/runs/'+self.run.run_id+'/proposals',json={'request_key':str(uuid4()),'arguments':{}},headers=HEADERS).status_code,422)
            self.assertEqual(client.get('/api/proposals/'+str(uuid4())).status_code,404)

    def test_api_fault_point_then_lookup_only_recovers(self):
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            first=self.proposal(client);self.approve(client,first)
            identifier=first['proposal']['proposal_id']
            op=self.post(client,'/proposals/'+identifier+'/operation',{})['operation']['operation_id']
            self.assertEqual(client.post('/api/operations/'+op+'/dispatch',json={'interrupt':'arbitrary'},headers=HEADERS).status_code,422)
            interrupted=self.post(client,'/operations/'+op+'/dispatch',{'interrupt':'after-send'})
            self.assertEqual(interrupted['status'],'DISPATCHING')
            self.assertEqual(client.post('/api/operations/'+op+'/dispatch',json={},headers=HEADERS).status_code,409)
            self.assertEqual(client.post('/api/operations/'+op+'/reconcile',json={},headers=HEADERS).status_code,409)
            wait_for_expired_lease(self.runs,op)
            recovered=self.post(client,'/operations/'+op+'/reconcile',{})
            self.assertEqual(recovered['status'],'SUCCEEDED')
            events=client.get('/api/operations/'+op).json()['events']
            self.assertEqual([event['status'] for event in events],['PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED'])

    def test_decision_cannot_mutate_another_operators_proposal_before_returning_404(self):
        from dataclasses import replace
        from harness.contracts import Run
        from harness.composition import compose
        other=replace(self.operator,actor_id='other-operator')
        profile=self.run.execution_profile
        run=self.runs.create(Run(other.workspace_id,other.actor_id,execution_profile=profile),other)
        asyncio.run(compose(profile,other,self.runs).harness.execute(run.run_id,other))
        foreign=self.store.create(run.run_id,other,str(uuid4()))
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            response=client.post('/api/proposals/'+foreign['proposal_id']+'/decision',headers=HEADERS,json={
                'payload_hash':foreign['payload_hash'],'expected_version':0,'decision':'APPROVED'})
            self.assertEqual(response.status_code,404)
        self.assertEqual(self.store.get(foreign['proposal_id'],other)['status'],'PENDING_APPROVAL')

    def test_manual_resolution_closes_without_effect_and_requires_exact_review_hash(self):
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            first=self.proposal(client);self.approve(client,first)
            op=self.post(client,'/proposals/'+first['proposal']['proposal_id']+'/operation',{})['operation']['operation_id']
            self.post(client,'/operations/'+op+'/dispatch',{'interrupt':'before-send'})
            wait_for_expired_lease(self.runs,op)
            unknown=self.post(client,'/operations/'+op+'/reconcile',{})
            self.assertEqual(unknown['status'],'UNKNOWN')
            review=self.post(client,'/operations/'+op+'/resolution',{})
            self.assertEqual(review['payload']['conclusion'],'NOT_APPLIED')
            self.assertEqual(self.post(client,'/operations/'+op+'/resolution',{}),review)
            request={'payload_hash':review['payload_hash'],'expected_version':0}
            self.assertEqual(client.post('/api/operations/'+op+'/resolution/confirm',json=request|{'conclusion':'NOT_APPLIED'},headers=HEADERS).status_code,422)
            self.assertEqual(client.post('/api/operations/'+op+'/resolution/confirm',json=request|{'payload_hash':'0'*64},headers=HEADERS).status_code,409)
            confirmed=self.post(client,'/operations/'+op+'/resolution/confirm',request)
            self.assertEqual(confirmed['status'],'CONFIRMED')
            self.assertEqual(self.post(client,'/operations/'+op+'/resolution/confirm',request),confirmed)
            operation=client.get('/api/operations/'+op).json()
            self.assertEqual(operation['status'],'NOT_APPLIED')
            self.assertEqual(operation['events'][-1]['actor_id'],'approver-demo')
            self.assertEqual(self.post(client,'/operations/'+op+'/dispatch',{})['status'],'NOT_APPLIED')
        from harness.jenkins_simulator import JenkinsSimulator
        with JenkinsSimulator(self.path)._db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM requests').fetchone()['n'],0)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM closures').fetchone()['n'],1)
