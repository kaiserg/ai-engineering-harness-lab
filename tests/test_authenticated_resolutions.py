"""Capacidad de cierre sin request y cliente de revisión sin credenciales en salida."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from uuid import uuid4
from harness.authenticated_resolutions import ClosureProvider,ManualResolutionOperations
from harness.contracts import ContractError,Identity
from harness.user_write_worker import ServiceWriteProvider
from scripts import users,user_proposals


class AuthenticatedResolutionBoundaryTests(unittest.TestCase):
    def test_normal_worker_has_no_unreviewed_closure_capability(self):
        scope=Identity('reader-demo','workspace-alpha',frozenset(),frozenset())
        provider=ServiceWriteProvider(scope,Path('/private/tmp/unused-closure.sqlite3'))
        with patch.object(provider.client,'close_request') as close:
            with self.assertRaises(ContractError):provider.close_request(scope,'id','hash')
        close.assert_not_called()

    def test_manual_closure_capability_cannot_execute_or_reconcile_requests(self):
        scope=Identity('reader-demo','workspace-alpha',frozenset(),frozenset())
        provider=ClosureProvider(scope,Path('/private/tmp/unused-closure.sqlite3'),closure_authorizer=lambda:None)
        ops=ManualResolutionOperations.__new__(ManualResolutionOperations)
        for action in (lambda:provider.request(scope),lambda:provider.lookup(scope),lambda:ops.dispatch('id',scope),lambda:ops.reconcile('id',scope)):
            with self.assertRaises(ContractError) as denied:action()
            self.assertEqual(denied.exception.code,'FORBIDDEN')

    def test_cli_submits_review_hash_and_version_once_without_selecting_a_reviewer(self):
        identifier,session=str(uuid4()),str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);users.save_credential(runtime/'user-sessions',{'session_id':session,'token':'PRIVATE_SENTINEL'})
            response=MagicMock(status_code=200);response.json.return_value={'resolution':{'status':'CONFIRMED'}}
            output=StringIO()
            with patch('sys.argv',['user-proposals','--session',session,'confirm-resolution',identifier,'--payload-hash','a'*64,'--version','0']),patch.object(user_proposals,'RUNTIME',runtime),patch('httpx.Client') as factory,redirect_stdout(output):
                factory.return_value.__enter__.return_value.request.return_value=response
                self.assertEqual(user_proposals.main(),0)
            factory.assert_called_once_with(trust_env=False,follow_redirects=False,timeout=10)
            request=factory.return_value.__enter__.return_value.request
            request.assert_called_once()
            self.assertEqual(request.call_args.args,('POST','http://127.0.0.1:8005/api/operations/'+identifier+'/resolution/confirm'))
            self.assertEqual(request.call_args.kwargs['json'],{'payload_hash':'a'*64,'expected_version':0})
            self.assertNotIn('PRIVATE_SENTINEL',output.getvalue())
