"""Rotación en vivo de Compose con JWT anterior y run pausado conservado."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from test_stack import api, wait_for, docker
from harness.postgres_config import RUNTIME
from harness.security_lifecycle import status
from harness.service_tls import trust_context, material_directory

ROOT = Path(__file__).resolve().parents[1]


def maintenance(action):
    # Salida solo de metadatos públicos; no imprimir tokens de la prueba.
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/security_live.py'), action],
        cwd=ROOT, capture_output=True, text=True, timeout=150)
    if result.returncode:
        raise AssertionError('Security maintenance failed: ' + result.stderr[-1500:])
    return json.loads(result.stdout)


def token():
    import base64
    secret = (material_directory(RUNTIME) / 'mcp-client-secret').read_text().strip()
    request = urllib.request.Request('https://127.0.0.1:8011/token',
        data=b'grant_type=client_credentials&scope=jira:read',
        headers={'Authorization':'Basic '+base64.b64encode(('harness-worker:'+secret).encode()).decode(),
                 'Content-Type':'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(request, context=trust_context(), timeout=5) as response:
        return json.load(response)['access_token']


def mcp_list(credential):
    request = urllib.request.Request('https://127.0.0.1:8010/mcp/',
        data=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).encode(),
        headers={'Authorization':'Bearer '+credential, 'Content-Type':'application/json',
                 'Accept':'application/json, text/event-stream'})
    with urllib.request.urlopen(request, context=trust_context(), timeout=5) as response:
        payload = json.load(response)
    if 'result' not in payload:
        raise AssertionError('MCP list failed')
    return payload


class RotationComposeTests(unittest.TestCase):
    def test_certificates_signing_overlap_and_run_recovery(self):
        before_ids = docker('ps','-q','identity','jira-mcp','worker')
        current = status()
        if any(item['retire_at'] > time.time() for item in current['previous']):
            self.fail('Previous rotation still within grace window; wait until retire_at before this suite.')
        run_id = api('/runs', {'scenario':'matching','pause_at':'jira'})['run_id']
        paused = wait_for(run_id, lambda run: run['job']['status'] == 'PAUSED')
        before = status()
        renewed = maintenance('renew-certificates')
        self.assertEqual(before['signing_kid'], renewed['signing_kid'])
        for service in ('jira','identity'):
            self.assertNotEqual(before['certificates'][service]['serial'], renewed['certificates'][service]['serial'])
        old_token = token()
        import jwt
        old_claims = jwt.decode(old_token, options={'verify_signature':False})
        rotated = maintenance('rotate-signing-key')
        self.assertNotEqual(rotated['signing_kid'], renewed['signing_kid'])
        self.assertEqual(rotated['previous'][0]['kid'], renewed['signing_kid'])
        self.assertLess(time.time(), old_claims['exp'], 'Maintenance exceeded token TTL; overlap proof needs fresh credentials')
        self.assertIn('tools', mcp_list(old_token)['result'])
        self.assertIn('tools', mcp_list(token())['result'])
        loaded = api('/runs/'+run_id)
        self.assertEqual(loaded['results'], paused['results'])
        api('/runs/'+run_id+'/resume', {})
        done = wait_for(run_id, lambda run: run['job']['status'] == 'DONE')
        self.assertEqual(done['results'][0], paused['results'][0])
        self.assertEqual((done['model_calls'],done['tool_calls']),(4,3))
        self.assertEqual(done['assessment']['status'],'VERIFIED')
        self.assertFalse((RUNTIME / 'security-live-operation.json').exists())
        self.assertEqual(before_ids,docker('ps','-q','identity','jira-mcp','worker'))
        # Verificar que la clave CA privada sigue fuera del worker.
        output = docker('exec','-T','worker','python','-c',
            "from pathlib import Path; assert not Path('/app/data/runtime/tls-ca-key.pem').exists(); print('isolated')")
        self.assertIn('isolated', output)
