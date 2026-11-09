"""HTTP solo admite: el envío ocurre en un proceso independiente, con API detenida."""
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from unittest.mock import patch
import test_approval_web as fixtures
from lease_wait import wait_for_expired_lease
from harness.web import create_app
from harness.postgres_config import local_dsn
from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite
from harness.jenkins_simulator import JenkinsSimulator

ROOT=Path(__file__).resolve().parents[1]
HEADERS={'X-Harness-Client':'local-ui'}


class WriteWorkerWebTests(unittest.TestCase):
    setUp=fixtures.ApprovalWebTests.setUp
    tearDown=fixtures.ApprovalWebTests.tearDown
    post=fixtures.ApprovalWebTests.post
    proposal=fixtures.ApprovalWebTests.proposal
    approve=fixtures.ApprovalWebTests.approve

    def app(self):
        return create_app(storage='postgres',pg_dsn=local_dsn(),pg_schema=self.schema,
            approvals_demo=True,write_worker=True,approval_provider_path=self.path)

    def prepare(self,client):
        proposal=self.proposal(client);self.approve(client,proposal)
        return self.post(client,'/proposals/'+proposal['proposal']['proposal_id']+'/operation',{})['operation']['operation_id']

    def worker(self):
        result=subprocess.run([sys.executable,'-m','harness.write_worker','--once','--schema',self.schema,
            '--provider-db',str(self.path)],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_http_enqueues_without_sending_and_worker_runs_with_api_closed(self):
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            self.assertTrue(client.get('/api/config').json()['write_worker'])
            identifier=self.prepare(client)
            with patch.object(MCPJenkinsWrite,'request',side_effect=AssertionError('API must not send')) as request:
                response=client.post('/api/operations/'+identifier+'/dispatch',headers=HEADERS,json={})
                self.assertEqual(response.status_code,202)
                queued=response.json();self.assertEqual(queued['operation']['status'],'PREPARED')
                self.assertEqual(queued['write_job']['status'],'PENDING')
                self.assertEqual(self.post(client,'/operations/'+identifier+'/dispatch',{}),queued)
                request.assert_not_called()
        self.worker()
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            data=client.get('/api/runs/'+self.run.run_id+'/proposals').json()['items']
            item=next(item for item in data if item['operation'] and item['operation']['operation_id']==identifier)
            self.assertEqual(item['operation']['status'],'SUCCEEDED')
            self.assertEqual(item['write_job']['status'],'DONE')
            self.assertNotIn('owner',item['write_job']);self.assertNotIn('fencing_token',item['write_job'])
        with JenkinsSimulator(self.path)._db() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM requests').fetchone()['n'],1)

    def test_interrupted_worker_job_resumes_by_lookup_with_original_operation(self):
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            identifier=self.prepare(client)
            self.post(client,'/operations/'+identifier+'/dispatch',{'interrupt':'after-send'})
            response=client.post('/api/operations/'+identifier+'/dispatch',headers=HEADERS,json={'interrupt':'before-send'})
            self.assertEqual(response.status_code,409)
        self.worker()  # Punto didáctico: deja DISPATCHING y difiere el mismo trabajo.
        wait_for_expired_lease(self.runs,identifier)
        self.worker()  # No reenvía: consulta lo ya aceptado.
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            operation=client.get('/api/operations/'+identifier).json()
            self.assertEqual(operation['status'],'SUCCEEDED')
            self.assertEqual([e['status'] for e in operation['events']],['PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED'])
        with JenkinsSimulator(self.path)._db() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM requests').fetchone()['n'],1)

    def test_claim_fields_cannot_be_supplied_by_browser(self):
        with TestClient(self.app(),base_url='http://127.0.0.1') as client:
            identifier=self.prepare(client)
            for injected in ({'owner':'evil'},{'fencing_token':100},{'lease_seconds':999},{'worker_id':'other'}):
                response=client.post('/api/operations/'+identifier+'/dispatch',headers=HEADERS,json=injected)
                self.assertEqual(response.status_code,422)
        with self.assertRaises(ValueError):create_app(write_worker=True)
