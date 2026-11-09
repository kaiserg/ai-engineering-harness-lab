from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from harness.composition import compose, demo_identity
from harness.contracts import ContractError, Identity, Run
from harness.ownership import database_owner
from harness.web import create_app

HEADERS = {'X-Harness-Client': 'local-ui'}
ROOT = Path(__file__).parents[1]


def wait_for_run(client, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get('/api/runs/' + run_id)
        assert response.status_code == 200, response.text
        run = response.json()
        if not run['is_active']:
            return run
        time.sleep(0.02)
    raise AssertionError('El run no terminó dentro del tiempo de la prueba.')


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'runs.sqlite3'
        self.app = create_app(self.db)
        self.client = self.enterContext(TestClient(self.app, base_url='http://127.0.0.1'))

    def start(self, scenario='matching', pause_at='none'):
        response = self.client.post('/api/runs', json={'scenario': scenario, 'pause_at': pause_at}, headers=HEADERS)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['run_id']

    def test_frontend_and_api_load_without_external_assets(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('De la evidencia', response.text)
        self.assertIn("script-src 'self'", response.headers['content-security-policy'])
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        for asset in ('app.js', 'styles.css', 'favicon.svg'):
            self.assertEqual(self.client.get('/assets/' + asset).status_code, 200)
        self.assertEqual(len(self.client.get('/api/config').json()['scenarios']), 7)

    def test_all_scenarios_use_real_mcp_and_existing_core_rules(self):
        for scenario, expected in [('matching', 'VERIFIED'), ('stale', 'STALE_BUILD'), ('failed', 'BUILD_FAILED'),
                                   ('unavailable', 'INCOMPLETE'), ('injection-claim', 'STALE_BUILD')]:
            with self.subTest(scenario=scenario):
                run = wait_for_run(self.client, self.start(scenario))
                self.assertEqual(run['status'], 'COMPLETED', run)
                self.assertEqual(run['assessment']['status'], expected)
                self.assertEqual((run['model_calls'], run['tool_calls']), (4, 3))
                self.assertFalse(run['can_resume'])
        for scenario, expected in [('injection-job', 'FORBIDDEN'), ('injection-shell', 'UNKNOWN_TOOL')]:
            run = wait_for_run(self.client, self.start(scenario))
            self.assertEqual(run['status'], 'FAILED')
            self.assertEqual(run['outcome_code'], expected)
            self.assertEqual(len(run['results']), 1)

    def test_pause_resume_reuses_confirmed_results_limits_and_call_ids(self):
        run_id = self.start('stale', 'github')
        paused = wait_for_run(self.client, run_id)
        self.assertTrue(paused['can_resume'])
        self.assertEqual(len(paused['results']), 2)
        self.assertEqual(paused['checkpoint'], 'READY')
        response = self.client.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
        self.assertEqual(response.status_code, 202)
        done = wait_for_run(self.client, run_id)
        self.assertEqual(done['assessment']['status'], 'STALE_BUILD')
        self.assertEqual(done['results'][:2], paused['results'])
        self.assertEqual((done['model_calls'], done['tool_calls']), (4, 3))
        self.assertEqual(done['limits_snapshot'], paused['limits_snapshot'])
        cached = self.client.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS).json()
        self.assertFalse(cached['is_active'])
        self.assertEqual(cached['version'], done['version'])

    def test_saved_optional_failure_recovers_as_partial_report(self):
        run_id = self.start('unavailable', 'failure')
        paused = wait_for_run(self.client, run_id)
        self.assertTrue(paused['can_resume'])
        self.assertEqual(len(paused['tool_failures']), 1)
        self.client.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
        done = wait_for_run(self.client, run_id)
        self.assertEqual(done['assessment']['status'], 'INCOMPLETE')
        self.assertEqual(done['tool_failures'], paused['tool_failures'])
        self.assertEqual(done['tool_calls'], 3)

    def test_caller_cannot_supply_identity_permissions_profiles_or_resume_configuration(self):
        for extra in ({'roles': ['admin']}, {'workspace_id': 'workspace-beta'}, {'requested_by': 'admin'},
                      {'execution_profile': 'fake-jira-v1'}, {'active_timeout': 9999}, {'command': 'cat .env'}):
            response = self.client.post('/api/runs', json={'scenario': 'matching', **extra}, headers=HEADERS)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {'error': 'INVALID_REQUEST'})
        run_id = self.start(pause_at='jira')
        wait_for_run(self.client, run_id)
        response = self.client.post('/api/runs/' + run_id + '/resume', json={'roles': ['admin']}, headers=HEADERS)
        self.assertEqual(response.status_code, 422)

    def test_browser_boundary_rejects_cross_origin_and_missing_custom_header(self):
        for headers in ({}, {**HEADERS, 'Origin': 'https://untrusted.example'},
                        {**HEADERS, 'Sec-Fetch-Site': 'cross-site'}):
            response = self.client.post('/api/runs', json={}, headers=headers)
            self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/runs', json={'pause_at': 'jira'},
                                    headers={**HEADERS, 'Origin': 'http://127.0.0.1'})
        self.assertEqual(response.status_code, 202)
        wait_for_run(self.client, response.json()['run_id'])
        self.assertEqual(self.client.get('/api/runs', headers={'Host': 'untrusted.example'}).status_code, 400)

    def test_other_actor_and_workspace_do_not_appear_in_list_or_detail(self):
        service = self.app.state.service
        for identity in (replace(demo_identity(), actor_id='other-reader'),
                         replace(demo_identity(), workspace_id='workspace-beta')):
            run = service.store.create(Run(identity.workspace_id, identity.actor_id), identity)
            self.assertEqual(self.client.get('/api/runs/' + run.run_id).status_code, 404)
            self.assertNotIn(run.run_id, [r['run_id'] for r in self.client.get('/api/runs').json()['runs']])

    def test_revoked_resource_access_blocks_detail_and_filters_history(self):
        run_id = self.start()
        wait_for_run(self.client, run_id)
        self.app.state.service.identity = replace(demo_identity(), jobs=frozenset())
        self.assertEqual(self.client.get('/api/runs/' + run_id).status_code, 403)
        self.assertEqual(self.client.get('/api/runs').json()['runs'], [])

    def test_failed_run_cannot_be_resumed_and_errors_do_not_echo_input(self):
        run_id = self.start('injection-shell')
        wait_for_run(self.client, run_id)
        response = self.client.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'INVALID_TRANSITION')
        self.assertEqual(self.client.get('/api/runs/not-a-uuid').json(), {'error': 'INVALID_REQUEST'})
        self.assertEqual(self.client.get('/api/runs/' + str(uuid4())).status_code, 404)

    def test_invalid_pause_or_scenario_does_not_create_a_run(self):
        for body in ({'scenario': 'unknown'}, {'scenario': 'matching', 'pause_at': 'failure'}, {'pause_at': 2}):
            self.assertEqual(self.client.post('/api/runs', json=body, headers=HEADERS).status_code, 422)
        self.assertEqual(self.client.get('/api/runs').json()['runs'], [])

    def test_owner_lock_prevents_cli_or_second_server_using_same_database(self):
        with self.assertRaises(ContractError) as error:
            with database_owner(self.db):
                self.fail('No debe obtener otro dueño.')
        self.assertEqual(error.exception.code, 'DATABASE_IN_USE')
        process = subprocess.run([sys.executable, '-m', 'harness', '--db', str(self.db), 'start'],
                                 cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 1)
        self.assertIn('DATABASE_IN_USE', process.stdout)


class WebLifecycleTests(unittest.TestCase):
    def test_persisted_checkpoint_survives_server_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'runs.sqlite3'
            with TestClient(create_app(db), base_url='http://127.0.0.1') as client:
                run_id = client.post('/api/runs', json={'scenario': 'stale', 'pause_at': 'github'}, headers=HEADERS).json()['run_id']
                paused = wait_for_run(client, run_id)
            with TestClient(create_app(db), base_url='http://127.0.0.1') as client:
                loaded = client.get('/api/runs/' + run_id).json()
                self.assertEqual(loaded['results'], paused['results'])
                self.assertTrue(loaded['can_resume'])
                client.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
                self.assertEqual(wait_for_run(client, run_id)['assessment']['status'], 'STALE_BUILD')

    def test_duplicate_resume_is_rejected_while_run_is_active(self):
        def slow_compose(*args, **kwargs):
            return compose(*args, **kwargs, delay=0.15)
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(Path(directory) / 'runs.sqlite3', composer=slow_compose), base_url='http://127.0.0.1') as client:
                run_id = client.post('/api/runs', json={'pause_at': 'github'}, headers=HEADERS).json()['run_id']
                wait_for_run(client, run_id)
                first = client.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
                second = client.post('/api/runs/' + run_id + '/resume', json={}, headers=HEADERS)
                self.assertEqual(first.status_code, 202)
                self.assertEqual(second.status_code, 409)
                self.assertEqual(second.json()['error'], 'RUN_BUSY')
                self.assertEqual(wait_for_run(client, run_id)['tool_calls'], 3)

    def test_runs_are_serial_even_when_multiple_requests_arrive(self):
        active, maximum = 0, 0
        def tracking_compose(*args, **kwargs):
            execution = compose(*args, **kwargs)
            original = execution.harness.execute
            async def execute(*params):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                try:
                    return await original(*params)
                finally:
                    active -= 1
            execution.harness.execute = execute
            return execution
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(Path(directory) / 'runs.sqlite3', composer=tracking_compose), base_url='http://127.0.0.1') as client:
                ids = [client.post('/api/runs', json={}, headers=HEADERS).json()['run_id'] for _ in range(2)]
                for run_id in ids:
                    self.assertEqual(wait_for_run(client, run_id)['status'], 'COMPLETED')
                self.assertEqual(maximum, 1)

    def test_pending_queue_has_a_limit_and_does_not_create_an_extra_run(self):
        def slow_compose(*args, **kwargs):
            return compose(*args, **kwargs, delay=10)
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(Path(directory) / 'runs.sqlite3', composer=slow_compose), base_url='http://127.0.0.1') as client:
                for _ in range(16):
                    self.assertEqual(client.post('/api/runs', json={}, headers=HEADERS).status_code, 202)
                response = client.post('/api/runs', json={}, headers=HEADERS)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.json()['error'], 'QUEUE_FULL')
                self.assertEqual(len(client.get('/api/runs').json()['runs']), 16)
