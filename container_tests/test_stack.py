"""Pruebas reales de Compose; requieren scripts/stack.py up y ningún worker extra del host."""
import json
from pathlib import Path
import subprocess
import time
import unittest
import urllib.error
import urllib.request

from scripts.stack import compose_command

BASE = 'http://127.0.0.1:8002'


def api(path, body=None):
    request = urllib.request.Request(BASE + '/api' + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', 'X-Harness-Client': 'local-ui'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def docker(*args):
    return subprocess.check_output(compose_command() + list(args), text=True, stderr=subprocess.STDOUT, timeout=90)


def wait_for(run_id, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = api('/runs/' + run_id)
        if predicate(run):
            return run
        time.sleep(0.1)
    raise AssertionError('El run no alcanzó el estado esperado en Compose.')


class ComposeTests(unittest.TestCase):
    def test_linux_mcp_scenarios_and_core_controls(self):
        for scenario, status, assessment in [('matching', 'COMPLETED', 'VERIFIED'),
                ('stale', 'COMPLETED', 'STALE_BUILD'), ('unavailable', 'COMPLETED', 'INCOMPLETE'),
                ('injection-job', 'FAILED', None)]:
            with self.subTest(scenario=scenario):
                run_id = api('/runs', {'scenario': scenario})['run_id']
                run = wait_for(run_id, lambda run: run['job']['status'] in ('DONE', 'FAILED'))
                self.assertEqual(run['status'], status)
                self.assertEqual(run['assessment']['status'] if run['assessment'] else None, assessment)
                if scenario == 'injection-job':
                    self.assertEqual(run['outcome_code'], 'FORBIDDEN')
                    self.assertEqual(run['tool_calls'], 1)
                else:
                    self.assertEqual((run['model_calls'], run['tool_calls']), (4, 3))

    def test_paused_evidence_survives_api_and_worker_restart(self):
        run_id = api('/runs', {'scenario': 'stale', 'pause_at': 'github'})['run_id']
        before = wait_for(run_id, lambda run: run['job']['status'] == 'PAUSED')
        docker('restart', 'api', 'worker')
        docker('up', '-d', '--no-build', '--wait')
        loaded = api('/runs/' + run_id)
        self.assertEqual(loaded['results'], before['results'])
        self.assertTrue(loaded['can_resume'])
        api('/runs/' + run_id + '/resume', {})
        done = wait_for(run_id, lambda run: run['job']['status'] == 'DONE')
        self.assertEqual(done['assessment']['status'], 'STALE_BUILD')
        self.assertEqual(done['results'][:2], before['results'])
        self.assertEqual((done['tool_calls'], done['job']['attempts']), (3, 2))

    def test_producer_keeps_work_durable_while_worker_is_stopped(self):
        docker('stop', 'worker')
        try:
            run_id = api('/runs', {})['run_id']
            time.sleep(0.3)
            waiting = api('/runs/' + run_id)
            self.assertEqual((waiting['status'], waiting['job']['status'], waiting['model_calls']), ('QUEUED', 'PENDING', 0))
        finally:
            docker('start', 'worker')
        done = wait_for(run_id, lambda run: run['job']['status'] == 'DONE')
        self.assertEqual(done['assessment']['status'], 'VERIFIED')

    def test_services_drop_root_and_image_contains_no_host_data(self):
        for service, module in [('api', 'harness.web'), ('worker', 'harness.worker')]:
            code = """
import json,os
from pathlib import Path
module = %r
found = []
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = path.read_bytes().split(b'\\0')
        if len(args)>2 and args[1:3] == [b'-m',module.encode()]:
            status = dict(line.split(':',1) for line in (path.parent/'status').read_text().splitlines())
            assert set(status['Uid'].split()) == {'10001'}, status['Uid']
            assert int(status['CapEff'].strip(),16) == 0
            assert status['NoNewPrivs'].strip() == '1'
            found.append(str(path.parent))
    except (PermissionError,FileNotFoundError):
        pass
assert found, 'service process not found'
p=Path('/app/data/runtime/postgres-app-password')
assert p.stat().st_mode & 0o777 == 0o600 and p.stat().st_uid == 10001
assert not Path('/app/.env').exists() and not Path('/app/.git').exists()
assert not Path('/app/data/runtime/harness.sqlite3').exists()
assert Path('/app/bin/jenkins-mcp').read_bytes()[:4] == b'\\x7fELF'
print('runtime verified')
""" % module
            self.assertIn('runtime verified', docker('exec', '-T', '--user', '10001:10001', service, 'python', '-c', code))
        # Ver la imagen sin mounts/entrypoint: la copia del secreto solo existe en el tmpfs de runtime.
        command = compose_command()
        base = command[:command.index('compose')]
        code = "from pathlib import Path; assert not Path('/app/data/runtime/postgres-app-password').exists(); assert not Path('/app/.env').exists(); assert not Path('/app/data/runtime/mcp-worker-private.pem').exists(); assert not Path('/app/data/runtime/mcp-worker-public.pem').exists(); print('image verified')"
        output = subprocess.check_output(base + ['run', '--rm', '--network', 'none', '--read-only',
            '--entrypoint', 'python', 'ai-engineering-harness-lab:local', '-c', code], text=True, timeout=30)
        self.assertIn('image verified', output)

    def test_sigterm_allows_current_read_run_to_finish(self):
        run_id = api('/runs', {})['run_id']
        wait_for(run_id, lambda run: run['job']['status'] == 'RUNNING' and run['model_calls'] > 0)
        try:
            docker('stop', 'worker')
            done = api('/runs/' + run_id)
            self.assertEqual((done['status'], done['job']['status']), ('COMPLETED', 'DONE'))
        finally:
            docker('start', 'worker')

    def test_http_profile_service_isolation_and_jira_restart_recovery(self):
        import urllib.error
        try:
            urllib.request.urlopen(urllib.request.Request('https://127.0.0.1:8010/mcp/', data=b'{}',
                headers={'Content-Type': 'application/json'}), timeout=5, context=__import__('harness.service_tls', fromlist=['trust_context']).trust_context())
            self.fail('Anonymous MCP request accepted')
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)
        files = {
            'api': (['postgres-app-password'], ['mcp-worker-private.pem', 'mcp-worker-public.pem',
                'issuer-signing.pem', 'mcp-client-secret', 'tls-ca-key.pem']),
            'worker': (['postgres-app-password', 'mcp-client-secret', 'tls-ca.pem'],
                ['mcp-worker-private.pem', 'issuer-signing.pem', 'tls-ca-key.pem', 'jira-key.pem', 'identity-key.pem']),
            'jira-mcp': (['tls-ca.pem', 'jira-cert.pem', 'jira-key.pem'],
                ['postgres-app-password', 'mcp-worker-private.pem', 'issuer-signing.pem', 'mcp-client-secret', 'tls-ca-key.pem']),
            'identity': (['identity-cert.pem', 'identity-key.pem', 'issuer-signing.pem', 'mcp-client-secret'],
                ['postgres-app-password', 'jira-key.pem', 'tls-ca-key.pem'])}
        for service, (present, absent) in files.items():
            code = ("from pathlib import Path; p=Path('/app/data/runtime'); "
                + f"assert all((p/n).exists() for n in {present!r}); "
                + f"assert all(not (p/n).exists() for n in {absent!r}); "
                + f"assert all((p/n).stat().st_mode & 0o777 == 0o600 for n in {present!r}); "
                + "print('isolated')")
            self.assertIn('isolated', docker('exec', '-T', '--user', '10001:10001', service, 'python', '-c', code))
        run_id = api('/runs', {'scenario': 'matching', 'pause_at': 'jira'})['run_id']
        before = wait_for(run_id, lambda run: run['job']['status'] == 'PAUSED')
        # Revisar el perfil durable sin leer ni imprimir credenciales.
        from harness.postgres_config import local_dsn
        from harness.postgres_store import PostgresRunStore
        from harness.composition import demo_identity
        saved = PostgresRunStore(local_dsn()).load(run_id, demo_identity())
        self.assertEqual(saved.execution_profile, 'mcp-devops-ci-v4:matching:benign:ignore')
        self.assertNotIn('Bearer ', json.dumps(before))
        docker('restart', 'jira-mcp')
        docker('up', '-d', '--no-build', '--wait')
        api('/runs/' + run_id + '/resume', {})
        done = wait_for(run_id, lambda run: run['job']['status'] == 'DONE')
        self.assertEqual(done['results'][0], before['results'][0])
        self.assertEqual((done['model_calls'], done['tool_calls']), (4, 3))

    def test_tls_issuer_restart_and_pending_admission(self):
        from harness.service_tls import trust_context
        # El trust store del sistema no conoce nuestra CA: debe rechazarla.
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen('https://127.0.0.1:8011/jwks', timeout=5)
        with urllib.request.urlopen('https://127.0.0.1:8011/jwks', context=trust_context(), timeout=5) as response:
            jwks = json.load(response)
        self.assertIn(len(jwks['keys']), (1, 2))
        self.assertNotIn('d', jwks['keys'][0])
        try:
            docker('stop', 'worker')
            run_id = api('/runs', {'scenario': 'matching'})['run_id']
            self.assertEqual(api('/runs/' + run_id)['job']['status'], 'PENDING')
            docker('restart', 'identity')
            docker('up', '-d', '--no-build', '--wait')
            after = wait_for(run_id, lambda run: run['job']['status'] == 'DONE')
            self.assertEqual(after['assessment']['status'], 'VERIFIED')
            self.assertEqual((after['model_calls'], after['tool_calls']), (4, 3))
        finally:
            docker('start', 'worker')
