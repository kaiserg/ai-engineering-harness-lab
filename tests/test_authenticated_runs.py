"""API de usuario sin fallback y cliente local sin exponer credenciales."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from uuid import uuid4
from fastapi.testclient import TestClient
from harness.web import create_app
from scripts import user_runs,users


class AuthenticatedBoundaryTests(unittest.TestCase):
    def test_reserved_schema_cannot_be_opened_by_fixture_api_or_worker(self):
        from harness.worker import Worker
        with self.assertRaises(ValueError):create_app(storage='postgres',pg_schema='harness_users_lab')
        store=MagicMock();store.schema='harness_users_lab'
        with self.assertRaises(ValueError):Worker(store)

    def test_unavailable_identity_directory_fails_before_admission(self):
        directory=MagicMock();directory.authenticate.side_effect=RuntimeError('PRIVATE_SENTINEL')
        with patch('harness.postgres_store.PostgresRunStore') as stores,patch('harness.authenticated_proposals.AuthenticatedProposals'):
            with TestClient(create_app(storage='postgres',user_auth=True,user_directory=directory),base_url='http://127.0.0.1') as client:
                response=client.post('/api/runs',headers={'Authorization':'Bearer PRIVATE_SENTINEL','X-Harness-Client':'local-ui'},json={})
            self.assertEqual(response.status_code,503)
            self.assertNotIn('PRIVATE_SENTINEL',response.text)
            stores.return_value.enqueue.assert_not_called()

    def test_cli_sends_only_scenario_and_never_puts_credential_in_url_or_stdout(self):
        identifier=str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);users.save_credential(runtime/'user-sessions',{'session_id':identifier,'token':'PRIVATE_SENTINEL'})
            response=MagicMock(status_code=202);response.json.return_value={'requested_by':'reader-demo'}
            output=StringIO()
            with patch('sys.argv',['user-runs','--session',identifier,'start','--scenario','stale']),patch.object(user_runs,'RUNTIME',runtime),patch('httpx.Client') as factory,redirect_stdout(output):
                factory.return_value.__enter__.return_value.request.return_value=response
                self.assertEqual(user_runs.main(),0)
            factory.assert_called_once_with(trust_env=False,follow_redirects=False,timeout=10)
            call=factory.return_value.__enter__.return_value.request.call_args
            self.assertEqual(call.args,('POST','http://127.0.0.1:8005/api/runs'))
            self.assertEqual(call.kwargs['json'],{'scenario':'stale','pause_at':'none'})
            self.assertNotIn('PRIVATE_SENTINEL',output.getvalue())
