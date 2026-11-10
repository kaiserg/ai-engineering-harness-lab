"""Frontera de identidad: errores cerrados y secretos fuera de stdout/URLs."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from uuid import uuid4
from fastapi.testclient import TestClient
from harness.contracts import ContractError
from harness.user_identity_api import create_app
from scripts import users


class UserIdentityBoundaryTests(unittest.TestCase):
    def test_database_failure_never_returns_an_identity_or_raw_exception(self):
        for error in (RuntimeError('PRIVATE_SENTINEL'),ContractError('DATABASE_UNAVAILABLE')):
            store=MagicMock();store.authenticate.side_effect=error
            with TestClient(create_app(store=store),base_url='http://127.0.0.1',raise_server_exceptions=False) as client:
                response=client.get('/api/me',headers={'Authorization':'Bearer PRIVATE_SENTINEL'})
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json(),{'error':'IDENTITY_UNAVAILABLE'})
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertNotIn('PRIVATE_SENTINEL',response.text)

    def test_issue_prints_metadata_only_and_private_file_failure_revokes_session(self):
        credential={'session_id':str(uuid4()),'token':'PRIVATE_SENTINEL','expires_at':'fixture'}
        for fail in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                runtime=Path(directory)/'data/runtime';store=MagicMock();store.issue.return_value=credential
                output=StringIO()
                with patch('sys.argv',['users','issue','--actor','reader-demo','--workspace','workspace-alpha']),patch.object(users,'RUNTIME',runtime),patch.object(users,'local_dsn',return_value='unused'),patch.object(users,'PostgresRunStore'),patch.object(users,'UserIdentityStore',return_value=store),redirect_stdout(output):
                    if fail:
                        with patch.object(users,'save_credential',side_effect=OSError),self.assertRaises(OSError):users.main()
                        store.revoke.assert_called_once_with(credential['session_id'])
                    else:
                        self.assertEqual(users.main(),0)
                        self.assertNotIn('PRIVATE_SENTINEL',output.getvalue())
                        self.assertNotIn('token',json.loads(output.getvalue()))
                        store.revoke.assert_not_called()

    def test_whoami_uses_fixed_loopback_no_proxies_or_redirects(self):
        identifier=str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)
            users.save_credential(runtime/'user-sessions',{'session_id':identifier,'token':'PRIVATE_SENTINEL'})
            response=MagicMock(status_code=200);response.json.return_value={'identity':{'actor_id':'reader-demo'}}
            with patch('sys.argv',['users','whoami',identifier]),patch.object(users,'RUNTIME',runtime),patch('httpx.Client') as factory,redirect_stdout(StringIO()):
                factory.return_value.__enter__.return_value.get.return_value=response
                self.assertEqual(users.main(),0)
            factory.assert_called_once_with(trust_env=False,follow_redirects=False,timeout=5)
            args=factory.return_value.__enter__.return_value.get.call_args
            self.assertEqual(args.args[0],'http://127.0.0.1:8004/api/me')
            self.assertEqual(args.kwargs['headers'],{'Authorization':'Bearer PRIVATE_SENTINEL'})
