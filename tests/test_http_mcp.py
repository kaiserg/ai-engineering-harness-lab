"""HTTP real, firma, frontera de permisos y recuperación con el mismo core."""
import asyncio
from dataclasses import replace
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import jwt

from harness.adapters.mcp_jira_http import MCPJiraHTTP
from harness.composition import compose, demo_identity
from harness.contracts import ContractError, Run, ToolCall
from harness.checkpoints import RunInterrupted
from harness.core import Limits
from harness.service_identity import prepare_keys, issue_token, verify_token
from harness.storage import SQLiteRunStore
from harness.web import worker_profile


class ServiceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        prepare_keys(self.directory)
        self.private = (self.directory / 'mcp-worker-private.pem').read_bytes()
        self.public = (self.directory / 'mcp-worker-public.pem').read_bytes()
        self.identity = demo_identity()

    def test_private_key_persists_and_public_key_cannot_sign(self):
        prepare_keys(self.directory)
        self.assertEqual(self.private, (self.directory / 'mcp-worker-private.pem').read_bytes())
        self.assertEqual((self.directory / 'mcp-worker-private.pem').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(Exception):
            issue_token(self.public, self.identity)

    def test_bad_signature_expiry_issuer_audience_and_algorithm_rejected(self):
        token = issue_token(self.private, self.identity)
        claims = jwt.decode(token, options={'verify_signature': False})
        for changes in ({'exp': int(time.time()) - 1}, {'aud': 'github'}, {'iss': 'other'},
                        {'iat': claims['iat'] - 1000}, {'sub': None}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                verify_token(jwt.encode(claims | changes, self.private, algorithm='EdDSA'), self.public)
        with self.assertRaises(ContractError):
            verify_token(jwt.encode(claims, 'fake' * 16, algorithm='HS256'), self.public)
        other = self.directory / 'other'
        prepare_keys(other)
        with self.assertRaises(ContractError):
            verify_token(token, (other / 'mcp-worker-public.pem').read_bytes())

    def test_claims_cannot_grant_permissions(self):
        claims = jwt.decode(issue_token(self.private, self.identity), options={'verify_signature': False})
        identity, fixture = verify_token(jwt.encode(claims | {'roles': ['admin'], 'projects': ['OTHER']},
            self.private, algorithm='EdDSA'), self.public)
        self.assertEqual(identity.roles, frozenset({'reader'}))
        self.assertEqual(identity.projects, frozenset({'DEV'}))
        for changes in ({'sub': 'api'}, {'scope': 'jira:write'}, {'actor': 'other'},
                        {'workspace': 'other'}, {'fixture': 'unknown'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ContractError, 'FORBIDDEN'):
                verify_token(jwt.encode(claims | changes, self.private, algorithm='EdDSA'), self.public)

    def test_only_new_worker_runs_use_http_profile(self):
        with patch.dict('os.environ', {'HARNESS_JIRA_TRANSPORT': 'http'}):
            self.assertEqual(worker_profile('mcp-devops-ci-v2:matching'), 'mcp-devops-ci-v4:matching:benign:ignore')
            self.assertEqual(worker_profile('mcp-devops-ci-v3:stale:injection:claim'), 'mcp-devops-ci-v4:stale:injection:claim')
        with patch.dict('os.environ', {'HARNESS_JIRA_TRANSPORT': 'typo'}):
            with self.assertRaisesRegex(ContractError, 'CONFIG_CHANGED'):
                worker_profile('mcp-devops-ci-v2:matching')


class HTTPMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        prepare_keys(self.directory)
        self.private = (self.directory / 'mcp-worker-private.pem').read_bytes()
        self.identity = demo_identity()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/mcp/'
        self.process = subprocess.Popen([sys.executable, '-c',
            'import sys,uvicorn; from pathlib import Path; from harness.jira_http_server import create_app; '
            'uvicorn.run(create_app(Path(sys.argv[1]).read_bytes(),fixture_delay=.3),host="127.0.0.1",port=int(sys.argv[2]),log_level="error",access_log=False)',
            str(self.directory / 'mcp-worker-public.pem'), str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_server)
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    if (await client.get(f'http://127.0.0.1:{port}/health')).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(.05)
            else:
                self.fail('MCP HTTP server did not start')
        self.patch_runtime = patch('harness.adapters.mcp_jira_http.RUNTIME', self.directory)
        self.patch_runtime.start()
        self.addCleanup(self.patch_runtime.stop)
        self.patch_env = patch.dict('os.environ', {'HARNESS_JIRA_MCP_URL': self.url})
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)

    def stop_server(self):
        self.process.terminate()
        self.process.wait(timeout=10)

    async def test_real_transport_schema_result_and_server_policy(self):
        adapter = MCPJiraHTTP(self.identity)
        result = await adapter.invoke(self.identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}))
        self.assertEqual(result.data['pull_request']['repository'], 'demo/atlas')
        self.assertEqual(adapter.invocations, 1)
        async with adapter.connection() as connection:
            response = await connection.session.call_tool('jira_get_issue', {'issue_key': 'OTHER-1'})
            self.assertTrue(response.is_error)
            self.assertEqual(response.structured_content['error']['code'], 'FORBIDDEN')
            response = await connection.session.call_tool('jira_get_issue', {'issue_key': 'DEV-123', 'roles': ['admin']})
            self.assertTrue(response.is_error)

    async def test_http_rejects_missing_invalid_token_origin_and_host(self):
        token = issue_token(self.private, self.identity)
        async with httpx.AsyncClient() as client:
            self.assertEqual((await client.post(self.url)).status_code, 401)
            self.assertEqual((await client.post(self.url, headers={'Authorization': 'Bearer invalid'})).status_code, 401)
            self.assertEqual((await client.post(self.url, headers={'Authorization': 'Bearer ' + token,
                'Origin': 'https://evil.example', 'Content-Type': 'application/json'})).status_code, 403)
            self.assertEqual((await client.post(self.url, headers={'Host': 'evil.example'})).status_code, 400)
            denied = issue_token(self.private, replace(self.identity, workspace_id='other'))
            self.assertEqual((await client.post(self.url, headers={'Authorization': 'Bearer ' + denied})).status_code, 403)

    async def test_client_preserves_auth_error_and_core_fails_closed(self):
        prepare_keys(self.directory / 'other')
        wrong = (self.directory / 'other/mcp-worker-private.pem').read_bytes()
        (self.directory / 'mcp-worker-private.pem').write_bytes(wrong)
        adapter = MCPJiraHTTP(self.identity)
        with self.assertRaisesRegex(ContractError, 'MCP_UNAUTHENTICATED'):
            await adapter.invoke(self.identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}))
        store = SQLiteRunStore(self.directory / 'runs.sqlite3')
        profile = 'mcp-devops-ci-v4:matching:benign:ignore'
        run = Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile)
        store.create(run, self.identity)
        result = await compose(profile, self.identity, store).harness.execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'MCP_UNAUTHENTICATED')
        self.assertEqual(result.tool_calls, 1)
        self.assertFalse(result.results)
        self.assertFalse(result.tool_failures)

    async def test_recovery_keeps_confirmed_http_result_and_profile(self):
        store = SQLiteRunStore(self.directory / 'runs.sqlite3')
        profile = 'mcp-devops-ci-v4:matching:benign:ignore'
        run = Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile)
        store.create(run, self.identity)
        def interrupt(name, run):
            if name == 'after-result' and len(run.results) == 1:
                raise RunInterrupted()
        with self.assertRaises(RunInterrupted):
            await compose(profile, self.identity, store, checkpoint_hook=interrupt).harness.execute(run.run_id, self.identity)
        saved = store.load(run.run_id, self.identity)
        execution = compose(profile, self.identity, store)
        result = await execution.harness.resume(run.run_id, self.identity)
        self.assertEqual(result.status.value, 'COMPLETED')
        self.assertEqual(result.results[0], saved.results[0])
        self.assertEqual(execution.adapters['jira'].connections, 0)
        self.assertEqual(result.tool_calls, 3)
        self.assertGreaterEqual(result.active_seconds, saved.active_seconds)

    async def test_timeout_covers_real_http_connection_and_tool(self):
        store = SQLiteRunStore(self.directory / 'timeout.sqlite3')
        profile = 'mcp-devops-ci-v4:matching:benign:ignore'
        run = Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile)
        store.create(run, self.identity)
        result = await compose(profile, self.identity, store,
            limits=Limits(tool_timeout=.15)).harness.execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UPSTREAM_TIMEOUT')
        self.assertEqual(result.tool_calls, 1)
        self.assertFalse(result.results)
        self.assertFalse(result.tool_failures)

    async def test_pending_http_read_reuses_call_id_and_charges_new_attempt(self):
        store = SQLiteRunStore(self.directory / 'pending.sqlite3')
        profile = 'mcp-devops-ci-v4:matching:benign:ignore'
        run = Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile)
        store.create(run, self.identity)
        def interrupt(name, run):
            if name == 'before-tool':
                raise RunInterrupted()
        with self.assertRaises(RunInterrupted):
            await compose(profile, self.identity, store, checkpoint_hook=interrupt).harness.execute(run.run_id, self.identity)
        saved = store.load(run.run_id, self.identity)
        result = await compose(profile, self.identity, store).harness.resume(run.run_id, self.identity)
        self.assertEqual(result.results[0].call_id, saved.pending_call.call_id)
        self.assertEqual(result.tool_calls, 4)
        self.assertEqual(result.status.value, 'COMPLETED')
        self.assertGreaterEqual(result.recovered_time_seconds, saved.pending_time_seconds)

    async def test_concurrent_requests_keep_fixture_context_separate(self):
        call = ToolCall('jira_get_issue', {'issue_key': 'DEV-123'})
        benign, injected = await asyncio.gather(
            MCPJiraHTTP(self.identity, content_scenario='benign').invoke(self.identity, call),
            MCPJiraHTTP(self.identity, content_scenario='injection').invoke(self.identity, call))
        self.assertNotEqual(benign.data['description'], injected.data['description'])
