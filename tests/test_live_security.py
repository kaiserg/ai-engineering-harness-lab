"""Recarga por rol, fases de publicación y política persistida de renovación."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from harness.live_materials import LiveMaterials, publish
from harness.live_security import perform_live
from harness.security_lifecycle import ensure_generation, prepare_change
from harness.security_schedule import configure, tick
from harness.service_tls import material_directory
from harness.signing_keys import public_jwk


class LiveSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.initial=ensure_generation(self.directory)

    def live(self,role):
        publish(role,self.initial,self.directory)
        return LiveMaterials(role,self.directory,self.directory/'live-delivery'/role/'bundle.json')

    def test_encrypted_role_delivery_reloads_cert_without_exposing_private_key(self):
        live=self.live('jira')
        before=live.snapshot()
        new=prepare_change('renew-certificates',self.directory)
        publish('jira',new,self.directory)
        live.reload()
        self.assertNotEqual(live.snapshot()['serial'],before['serial'])
        self.assertIsNot(live.snapshot()['context'],before['context'])
        envelope=(self.directory/'live-delivery/jira/bundle.json').read_bytes()
        self.assertNotIn(b'PRIVATE KEY',envelope)
        self.assertNotIn((new/'jira-key.pem').read_bytes(),envelope)
        self.assertEqual((self.directory/'jira-delivery-key').stat().st_mode & 0o777,0o600)

    def test_tampering_wrong_role_and_replay_do_not_replace_current_snapshot(self):
        live=self.live('identity')
        path=self.directory/'live-delivery/identity/bundle.json'
        first=path.read_bytes()
        new=prepare_change('renew-certificates',self.directory)
        publish('identity',new,self.directory)
        live.reload()
        current=live.snapshot()
        path.write_bytes(first)
        with self.assertRaises(ValueError):
            live.reload()
        self.assertIs(live.snapshot(),current)
        publish('jira',new,self.directory)
        path.write_bytes((self.directory/'live-delivery/jira/bundle.json').read_bytes())
        with self.assertRaises(Exception):
            live.reload()
        self.assertIs(live.snapshot(),current)

    def test_rotation_announces_before_switch_and_pins_retirement_on_recovery(self):
        published=[]
        fail=[True]
        def publisher(role,generation,directory,**kwargs):
            private=(generation/'issuer/mcp-worker-private.pem').read_bytes()
            published.append((public_jwk(private)['kid'],kwargs.get('advertised')))
            if len(published)==2 and fail[0]:
                raise OSError('crash after switching intent')
            return len(published)
        def waiter(role,predicate,directory):
            return {}
        with self.assertRaises(OSError):
            perform_live('rotate-signing-key',self.directory,publisher=publisher,waiter=waiter)
        journal=json.loads((self.directory/'security-live-operation.json').read_text())
        deadline=journal['switch_deadline']
        self.assertEqual(journal['phase'],'switching')
        self.assertEqual(len(published[0][1]),2)
        fail[0]=False
        with patch('harness.live_security.time.time',return_value=deadline+200):
            perform_live('recover',self.directory,publisher=publisher,waiter=waiter)
        self.assertTrue(all(item[1] is None for item in published[1:]))
        target=material_directory(self.directory)
        previous=json.loads((target/'issuer-previous.json').read_text())
        self.assertEqual(previous[0]['lab_retire_at'],deadline)
        self.assertFalse((self.directory/'security-live-operation.json').exists())

    def test_recover_during_announcement_finishes_same_generation(self):
        def broken(*args,**kwargs):
            raise OSError('interrupted announcement')
        with self.assertRaises(OSError):
            perform_live('rotate-signing-key',self.directory,publisher=broken,waiter=lambda *args:None)
        journal=json.loads((self.directory/'security-live-operation.json').read_text())
        self.assertEqual(journal['phase'],'announcing')
        perform_live('recover',self.directory,publisher=lambda *args,**kwargs:1,waiter=lambda *args:None)
        self.assertEqual(material_directory(self.directory).name,journal['target'])

    def test_jwks_ack_failure_leaves_current_pointer_and_journal(self):
        def timeout(*args,**kwargs):
            raise ValueError('ack timeout')
        with self.assertRaises(ValueError):
            perform_live('renew-certificates',self.directory,publisher=lambda *args,**kwargs:1,waiter=timeout)
        self.assertEqual(material_directory(self.directory),self.initial.resolve())
        self.assertTrue((self.directory/'security-live-operation.json').exists())

    def test_schedule_is_due_based_idempotent_and_persists_backoff(self):
        state=configure(self.directory)
        now=time.time()
        calls=[]
        result=tick(self.directory,now=now,executor=lambda action,directory:calls.append(action))
        self.assertEqual(calls,[])
        result=tick(self.directory,now=now+24*86400,executor=lambda action,directory:calls.append(action))
        self.assertEqual(calls,['renew-certificates'])
        tick(self.directory,now=now+24*86400+1,executor=lambda *args:self.fail('duplicate tick'))
        def fail(*args):
            raise ValueError('upstream unavailable')
        failed=tick(self.directory,now=now+24*86400+61,executor=fail)
        self.assertEqual(failed['last_error'],'SECURITY_SCHEDULE_FAILED')
        self.assertEqual(failed['failures'],1)
        self.assertGreater(failed['next_check'],failed['last_check'])

    def test_scheduler_recovers_pending_before_rotating_or_renewing(self):
        # Un journal real incompleto debe tomar prioridad sobre la edad de certificados.
        (self.directory/'security-live-operation.json').write_text(json.dumps({'action':'rotate-signing-key'}))
        calls=[]
        result=tick(self.directory,now=time.time()+40*86400,executor=lambda action,directory:calls.append(action))
        self.assertEqual(calls,['recover'])
        self.assertGreater(result['last_rotation'],configure(self.directory)['last_rotation']-1)

    def test_schedule_does_not_run_when_disabled(self):
        state=configure(self.directory)
        state['enabled']=False
        (self.directory/'security-schedule.json').write_text(json.dumps(state))
        tick(self.directory,now=time.time()+40*86400,executor=lambda *args:self.fail('disabled scheduler executed'))

    def test_jira_rejects_valid_token_with_stale_jwks_cache(self):
        from types import SimpleNamespace
        from fastapi.testclient import TestClient
        from harness.jira_http_server import create_app
        from harness.service_identity import issue_token
        from harness.identity_server import ISSUER
        from harness.composition import demo_identity
        from harness.signing_keys import parse_public_keys
        private=(self.initial/'issuer/mcp-worker-private.pem').read_bytes()
        jwk=public_jwk(private)
        token=issue_token(private,demo_identity(),issuer=ISSUER,kid=jwk['kid'])
        now=[0.0]
        with patch.dict('os.environ',{'HARNESS_SERVICE_IDENTITY':'issuer'}), patch(
                'harness.issuer_client.verification_key',return_value=parse_public_keys([jwk])):
            app=create_app(live=SimpleNamespace(),clock=lambda:now[0])
        now[0]=15.0
        client=TestClient(app,base_url='https://localhost')
        self.addCleanup(client.close)
        response=client.post('/mcp/',content=b'{}',headers={
            'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        self.assertEqual(response.status_code,401)

    def test_scheduler_process_stops_cooperatively_without_process_inspection(self):
        import subprocess,sys
        code=("from pathlib import Path; import threading; import scripts.security_live as m; "
            "from harness.security_schedule import configure,tick; "
            "p=Path(__import__('sys').argv[1]); m.RUNTIME=p; "
            "m.configure=lambda:configure(p); m.tick=lambda:tick(p); "
            "t=threading.Timer(10,lambda:(p/'security-scheduler.stop').write_text('{}')); "
            "t.daemon=True; t.start(); m.daemon()")
        process=subprocess.Popen([sys.executable,'-c',code,str(self.directory)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        from scripts import security_live
        with patch.object(security_live,'RUNTIME',self.directory), patch.object(
                security_live,'configure',lambda:configure(self.directory)):
            until=time.monotonic()+5
            while time.monotonic()<until and not security_live.scheduler_running():
                time.sleep(.05)
            self.assertTrue(security_live.scheduler_running())
            security_live.schedule_stop()
        self.assertEqual(process.wait(timeout=5),0)
        self.assertFalse(configure(self.directory)['enabled'])
