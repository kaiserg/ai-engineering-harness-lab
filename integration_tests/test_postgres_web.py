from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import sql

from harness.composition import demo_identity
from harness.contracts import Run
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.web import create_app

ROOT = Path(__file__).parents[1]
HEADERS = {'X-Harness-Client': 'local-ui'}


class PostgresWebTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'harness_it_' + uuid4().hex
        self.store = PostgresRunStore(local_dsn(), schema=self.schema)
        self.identity = demo_identity()

    def tearDown(self):
        assert self.schema.startswith('harness_it_') and len(self.schema) == 43
        with self.store._database() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def app(self, **kwargs):
        return create_app(storage='postgres', pg_dsn=local_dsn(), pg_schema=self.schema, **kwargs)

    def worker(self):
        result = subprocess.run([sys.executable, '-m', 'harness.worker', '--schema', self.schema, '--once'],
                                cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_api_is_only_producer_and_independent_process_executes(self):
        def forbidden_composer(*args, **kwargs):
            self.fail('La API no debe ejecutar el core en modo PostgreSQL.')
        with TestClient(self.app(composer=forbidden_composer), base_url='http://127.0.0.1') as client:
            response = client.post('/api/runs', json={}, headers=HEADERS)
            self.assertEqual(response.status_code, 202)
            queued = response.json()
            self.assertEqual((queued['status'], queued['job']['status'], queued['model_calls']), ('QUEUED', 'PENDING', 0))
            self.assertTrue(queued['is_active'])
            self.assertFalse(queued['can_resume'])
            self.assertEqual(client.get('/api/config').json()['execution_mode'], 'worker')
            self.worker()
            done = client.get('/api/runs/' + queued['run_id']).json()
            self.assertEqual((done['status'], done['job']['status']), ('COMPLETED', 'DONE'))
            self.assertEqual(done['assessment']['status'], 'VERIFIED')
            self.assertEqual((done['model_calls'], done['tool_calls']), (4, 3))

    def test_api_restart_preserves_pending_work_and_worker_does_not_need_api_alive(self):
        with TestClient(self.app(), base_url='http://127.0.0.1') as client:
            queued = client.post('/api/runs', json={'scenario': 'unavailable'}, headers=HEADERS).json()
        self.worker()  # Ningún servidor API está abierto.
        with TestClient(self.app(), base_url='http://127.0.0.1') as client:
            done = client.get('/api/runs/' + queued['run_id']).json()
            self.assertEqual(done['assessment']['status'], 'INCOMPLETE')
            self.assertEqual(done['job']['status'], 'DONE')
            self.assertEqual(len(done['tool_failures']), 1)

    def test_pause_and_requeue_do_not_reset_limits_results_or_call_ids(self):
        with TestClient(self.app(), base_url='http://127.0.0.1') as client:
            queued = client.post('/api/runs', json={'scenario': 'stale', 'pause_at': 'github'}, headers=HEADERS).json()
            self.worker()
            paused = client.get('/api/runs/' + queued['run_id']).json()
            self.assertTrue(paused['can_resume'])
            self.assertEqual(paused['job']['status'], 'PAUSED')
            response = client.post('/api/runs/' + queued['run_id'] + '/resume', json={}, headers=HEADERS)
            self.assertEqual(response.status_code, 202)
            self.worker()
            done = client.get('/api/runs/' + queued['run_id']).json()
            self.assertEqual(done['assessment']['status'], 'STALE_BUILD')
            self.assertEqual(done['results'][:2], paused['results'])
            self.assertEqual(done['limits_snapshot'], paused['limits_snapshot'])
            self.assertEqual((done['model_calls'], done['tool_calls']), (4, 3))
            self.assertEqual((done['job']['attempts'], done['job']['fencing_token']), (2, 2))

    def test_two_api_instances_cannot_duplicate_resume_admission(self):
        with TestClient(self.app(), base_url='http://127.0.0.1') as first, TestClient(self.app(), base_url='http://127.0.0.1') as second:
            run_id = first.post('/api/runs', json={'pause_at': 'jira'}, headers=HEADERS).json()['run_id']
            self.worker()
            one = first.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
            two = second.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
            self.assertEqual(one.status_code, 202)
            self.assertEqual(two.status_code, 409)
            self.assertEqual(two.json()['error'], 'RUN_BUSY')
            self.assertEqual(len(self.store.pending_jobs(self.identity)), 1)

    def test_client_cannot_supply_claim_owner_token_or_lease(self):
        with TestClient(self.app(), base_url='http://127.0.0.1') as client:
            for field, value in [('owner', 'admin'), ('fencing_token', 999), ('lease_seconds', 999),
                                 ('roles', ['admin']), ('workspace_id', 'workspace-beta')]:
                response = client.post('/api/runs', json={field: value}, headers=HEADERS)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()['error'], 'INVALID_REQUEST')
            self.assertEqual(client.get('/api/runs').json()['runs'], [])

    def test_policy_denial_is_a_failed_job_not_a_recoverable_provider_error(self):
        with TestClient(self.app(), base_url='http://127.0.0.1') as client:
            run_id = client.post('/api/runs', json={'scenario': 'injection-job'}, headers=HEADERS).json()['run_id']
            self.worker()
            run = client.get('/api/runs/' + run_id).json()
            self.assertEqual((run['status'], run['job']['status']), ('FAILED', 'FAILED'))
            self.assertEqual(run['outcome_code'], 'FORBIDDEN')
            self.assertEqual(run['job']['last_error'], 'FORBIDDEN')
            self.assertFalse(run['can_resume'])
            self.assertEqual(run['tool_failures'], [])

    def test_job_history_and_detail_remain_scoped_and_reauthorized(self):
        other = replace(self.identity, actor_id='other')
        foreign = self.store.enqueue(Run(other.workspace_id, other.actor_id), other)
        app = self.app()
        with TestClient(app, base_url='http://127.0.0.1') as client:
            self.assertEqual(client.get('/api/runs/' + foreign.run_id).status_code, 404)
            run_id = client.post('/api/runs', json={'pause_at': 'jenkins'}, headers=HEADERS).json()['run_id']
            self.worker()
            app.state.service.identity = replace(self.identity, jobs=frozenset())
            self.assertEqual(client.get('/api/runs/' + run_id).status_code, 403)
            self.assertEqual(client.get('/api/runs').json()['runs'], [])
