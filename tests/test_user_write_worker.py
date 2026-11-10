"""Alcance de servicio y cliente de admisión: sin credenciales en el job ni salida."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from uuid import uuid4
from harness.contracts import ContractError,Identity
from harness.user_write_worker import ServiceWriteProvider,create_worker
from scripts import users,user_proposals


class UserWriteBoundaryTests(unittest.TestCase):
    def test_provider_service_envelope_is_scoped_and_contains_no_user_credential(self):
        scope=Identity('reader-demo','workspace-alpha',frozenset(),frozenset())
        provider=ServiceWriteProvider(scope,Path('/private/tmp/unused-user-write.sqlite3'))
        other=Identity('reader-demo','workspace-beta',frozenset(),frozenset())
        with patch.object(provider.client,'request') as request:
            with self.assertRaises(ContractError):provider.request(other,'operation','hash',{},None)
        request.assert_not_called()
        self.assertNotEqual(scope.roles,provider.client.identity.roles)
        self.assertEqual(provider.client.identity.jobs,{'atlas-tests'})
        self.assertNotIn('token',str(provider.client.parameters.env).lower())

    def test_user_service_factory_cannot_target_fixture_schema(self):
        runs=MagicMock();runs.schema='harness_lab'
        with self.assertRaises(ValueError):create_worker(runs,MagicMock(),'reader-demo','workspace-alpha',Path('/private/tmp/unused'))

    def test_dispatch_cli_calls_admission_once_with_private_header(self):
        session,operation=str(uuid4()),str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);users.save_credential(runtime/'user-sessions',{'session_id':session,'token':'PRIVATE_SENTINEL'})
            response=MagicMock(status_code=202);response.json.return_value={'write_job':{'status':'PENDING'}}
            output=StringIO()
            with patch('sys.argv',['user-proposals','--session',session,'dispatch',operation,'--interrupt','after-send']),patch.object(user_proposals,'RUNTIME',runtime),patch('httpx.Client') as factory,redirect_stdout(output):
                factory.return_value.__enter__.return_value.request.return_value=response
                self.assertEqual(user_proposals.main(),0)
            request=factory.return_value.__enter__.return_value.request
            request.assert_called_once()
            self.assertEqual(request.call_args.args,('POST','http://127.0.0.1:8005/api/operations/'+operation+'/dispatch'))
            self.assertEqual(request.call_args.kwargs['json'],{'interrupt':'after-send'})
            self.assertNotIn('PRIVATE_SENTINEL',output.getvalue())
