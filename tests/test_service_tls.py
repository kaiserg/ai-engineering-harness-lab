"""TLS real + emisor separado; no verify=False ni tokens persistidos."""
from dataclasses import asdict
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import jwt
from fastapi.testclient import TestClient

from harness.adapters.mcp_jira_http import MCPJiraHTTP
from harness.checkpoints import RunInterrupted
from harness.composition import compose, demo_identity
from harness.contracts import ContractError, Run, ToolCall
from harness.core import Limits
from harness.identity_server import ISSUER, create_app
from harness.service_identity import issue_token
from harness.service_tls import prepare_tls, trust_context
from harness.storage import SQLiteRunStore, encode


def provision(directory):
    prepare_tls(directory)
    shutil.copyfile(directory / 'issuer/mcp-worker-private.pem', directory / 'issuer-signing.pem')
    (directory / 'issuer-signing.pem').chmod(0o600)


class TLSMaterialTests(unittest.TestCase):
    def test_material_persists_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            provision(directory)
            original = (directory / 'tls-ca.pem').read_bytes()
            prepare_tls(directory)
            self.assertEqual(original, (directory / 'tls-ca.pem').read_bytes())
            self.assertEqual((directory / 'tls-ca-key.pem').stat().st_mode & 0o777, 0o600)
            context = trust_context(directory)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_partial_material_and_wrong_private_key_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            provision(directory)
            (directory / 'jira-key.pem').write_bytes((directory / 'identity-key.pem').read_bytes())
            with self.assertRaises(ValueError):
                prepare_tls(directory)
            (directory / 'jira-key.pem').unlink()
            with self.assertRaises(ValueError):
                prepare_tls(directory)


class IssuerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        provision(self.directory)
        self.secret = (self.directory / 'mcp-client-secret').read_text()
        self.client = TestClient(create_app(self.directory), base_url='https://localhost')
        self.addCleanup(self.client.close)

    def test_client_authentication_scope_and_no_caller_selected_identity(self):
        body = {'grant_type': 'client_credentials', 'scope': 'jira:read'}
        self.assertEqual(self.client.post('/token', data=body).status_code, 401)
        self.assertEqual(self.client.post('/token', data=body, auth=('harness-worker', 'wrong')).status_code, 401)
        for changes in ({'scope': 'jira:write'}, {'actor': 'admin'}, {'workspace': 'other'},
                        {'audience': 'github'}, {'grant_type': 'password'}):
            self.assertEqual(self.client.post('/token', data=body | changes,
                auth=('harness-worker', self.secret)).status_code, 400)
        duplicate = self.client.post('/token', content='grant_type=client_credentials&scope=jira:read&scope=jira:write',
            headers={'Content-Type': 'application/x-www-form-urlencoded'}, auth=('harness-worker', self.secret))
        self.assertEqual(duplicate.status_code, 400)

    def test_jwks_public_only_and_token_uses_server_grants(self):
        response = self.client.post('/token', data={'grant_type': 'client_credentials', 'scope': 'jira:read'},
            auth=('harness-worker', self.secret))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        keys = self.client.get('/jwks').json()['keys']
        self.assertNotIn('d', keys[0])
        token = response.json()['access_token']
        self.assertEqual(jwt.get_unverified_header(token)['kid'], keys[0]['kid'])
        claims = jwt.decode(token, jwt.PyJWK.from_dict(keys[0]).key, algorithms=['EdDSA'], issuer=ISSUER, audience='jira-mcp')
        self.assertEqual((claims['sub'], claims['actor'], claims['workspace']),
            ('harness-worker', 'reader-demo', 'workspace-alpha'))
        self.assertEqual(claims['exp'] - claims['iat'], 60)


class TLSFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.tmp.name)
        provision(cls.directory)
        cls.processes = []
        cls.log = (cls.directory / 'server.log').open('w')
        def port():
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]
        cls.issuer_port, cls.jira_port = port(), port()
        cls.issuer_url = f'https://127.0.0.1:{cls.issuer_port}'
        cls.jira_url = f'https://127.0.0.1:{cls.jira_port}/mcp/'
        env = os.environ | {'HARNESS_SERVICE_IDENTITY': 'issuer', 'HARNESS_ISSUER_URL': cls.issuer_url}
        issuer_code = ('import sys,uvicorn; from pathlib import Path; from harness.identity_server import create_app; '
            'p=Path(sys.argv[1]); uvicorn.run(create_app(p),host="127.0.0.1",port=int(sys.argv[2]),'
            'ssl_keyfile=str(p/"identity-key.pem"),ssl_certfile=str(p/"identity-cert.pem"),access_log=False,log_level="error")')
        jira_code = ('import sys,uvicorn; from pathlib import Path; import harness.jira_http_server as s; '
            's.RUNTIME=Path(sys.argv[1]); uvicorn.run(s.create_app(),host="127.0.0.1",port=int(sys.argv[2]),'
            'ssl_keyfile=str(s.RUNTIME/"jira-key.pem"),ssl_certfile=str(s.RUNTIME/"jira-cert.pem"),access_log=False,log_level="error")')
        try:
            for code, selected_port, url in [(issuer_code, cls.issuer_port, cls.issuer_url + '/health'),
                    (jira_code, cls.jira_port, cls.jira_url.replace('/mcp/', '/health'))]:
                cls.processes.append(subprocess.Popen([sys.executable, '-c', code, str(cls.directory), str(selected_port)],
                    env=env, stdout=cls.log, stderr=cls.log))
                with httpx.Client(verify=trust_context(cls.directory), trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get(url).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(.05)
                    else:
                        raise RuntimeError((cls.directory / 'server.log').read_text())
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for process in cls.processes:
            process.terminate()
            process.wait(timeout=10)
        cls.log.close()
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.identity = demo_identity()
        self.env = patch.dict('os.environ', {'HARNESS_SERVICE_IDENTITY': 'issuer',
            'HARNESS_ISSUER_URL': self.issuer_url, 'HARNESS_JIRA_MCP_URL': self.jira_url})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runtime = patch('harness.adapters.mcp_jira_http.RUNTIME', self.directory)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    async def test_real_tls_issuer_and_mcp(self):
        adapter = MCPJiraHTTP(self.identity)
        result = await adapter.invoke(self.identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}))
        self.assertEqual(result.data['status'], 'IN_REVIEW')
        self.assertEqual(adapter.invocations, 1)
        # Sin clave de firma del worker en el runtime utilizado por el cliente.
        self.assertFalse((self.directory / 'mcp-worker-private.pem').exists())

    async def test_tls_rejects_unknown_ca_wrong_hostname_and_plain_http(self):
        async with httpx.AsyncClient(trust_env=False) as client:
            with self.assertRaises(httpx.TransportError):
                await client.get(self.issuer_url + '/jwks')
        with socket.create_connection(('127.0.0.1', self.jira_port), timeout=3) as sock:
            with self.assertRaises(ssl.SSLCertVerificationError):
                trust_context(self.directory).wrap_socket(sock, server_hostname='wrong-host')
        with patch.dict('os.environ', {'HARNESS_JIRA_MCP_URL': self.jira_url.replace('https:', 'http:')}):
            with self.assertRaisesRegex(ContractError, 'CONFIG_CHANGED'):
                MCPJiraHTTP(self.identity)

    async def test_jira_rejects_legacy_signature_wrong_kid_expired_audience_and_issuer(self):
        secret = (self.directory / 'mcp-client-secret').read_text()
        async with httpx.AsyncClient(verify=trust_context(self.directory), trust_env=False) as client:
            issued = (await client.post(self.issuer_url + '/token', auth=('harness-worker', secret),
                data={'grant_type': 'client_credentials', 'scope': 'jira:read'})).json()['access_token']
            claims = jwt.decode(issued, options={'verify_signature': False})
            kid = jwt.get_unverified_header(issued)['kid']
            private = (self.directory / 'issuer-signing.pem').read_bytes()
            variants = [issue_token(private, self.identity),
                jwt.encode(claims, private, algorithm='EdDSA', headers={'kid': 'unknown'})]
            for change in ({'aud': 'github'}, {'iss': 'wrong'}, {'exp': int(time.time()) - 1}):
                variants.append(jwt.encode(claims | change, private, algorithm='EdDSA', headers={'kid': kid}))
            for token in variants:
                response = await client.post(self.jira_url, content=b'{}',
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 401)

    async def test_issuer_failure_and_bad_credential_block_core(self):
        for unavailable in (False, True):
            store = SQLiteRunStore(self.directory / ('unavailable.sqlite3' if unavailable else 'bad-client.sqlite3'))
            profile = 'mcp-devops-ci-v4:matching:benign:ignore'
            run = Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile)
            store.create(run, self.identity)
            if unavailable:
                endpoint_patch = patch.dict('os.environ', {'HARNESS_ISSUER_URL': 'https://127.0.0.1:1'})
            else:
                endpoint_patch = patch('harness.issuer_client.RUNTIME', self.directory)
            original = (self.directory / 'mcp-client-secret').read_bytes()
            try:
                if not unavailable:
                    (self.directory / 'mcp-client-secret').write_text('bad')
                with endpoint_patch:
                    result = await compose(profile, self.identity, store).harness.execute(run.run_id, self.identity)
                self.assertEqual(result.outcome_code, 'MCP_ISSUER_UNAVAILABLE' if unavailable else 'MCP_UNAUTHENTICATED')
                self.assertEqual(result.tool_calls, 1)
                self.assertFalse(result.results)
                self.assertFalse(result.tool_failures)
                self.assertNotIn(original.decode(), encode(result))
            finally:
                (self.directory / 'mcp-client-secret').write_bytes(original)

    async def test_recovery_reuses_confirmed_result_and_refreshes_credentials_for_pending_read(self):
        for checkpoint in ('after-result', 'before-tool'):
            with self.subTest(checkpoint=checkpoint):
                store = SQLiteRunStore(self.directory / (checkpoint + '.sqlite3'))
                profile = 'mcp-devops-ci-v4:matching:benign:ignore'
                run = Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile,
                    limits_snapshot=asdict(Limits()))
                store.create(run, self.identity)
                def interrupt(name, state):
                    if name == checkpoint:
                        raise RunInterrupted()
                with self.assertRaises(RunInterrupted):
                    await compose(profile, self.identity, store, checkpoint_hook=interrupt).harness.execute(run.run_id, self.identity)
                before = store.load(run.run_id, self.identity)
                execution = compose(profile, self.identity, store)
                after = await execution.harness.resume(run.run_id, self.identity)
                self.assertEqual(after.status.value, 'COMPLETED')
                if checkpoint == 'after-result':
                    self.assertEqual(after.results[0], before.results[0])
                    self.assertEqual(execution.adapters['jira'].connections, 0)
                    self.assertEqual(after.tool_calls, 3)
                else:
                    self.assertEqual(after.results[0].call_id, before.pending_call.call_id)
                    self.assertEqual(after.tool_calls, 4)
                self.assertNotIn('access_token', encode(after))
                self.assertGreaterEqual(after.active_seconds, before.active_seconds)
