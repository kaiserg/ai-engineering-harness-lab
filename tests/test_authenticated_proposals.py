"""Revisión explícita, secretos fuera de salida y ausencia de despacho autenticado."""
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from uuid import uuid4
from harness.authenticated_proposals import AuthenticatedProposals
from harness.contracts import ContractError
from harness.development import fixture_identity
from harness.write_worker import WriteWorker
from scripts import user_proposals,users


class AuthenticatedProposalBoundaryTests(unittest.TestCase):
    def test_cli_uses_reviewed_hash_and_version_without_choosing_an_actor(self):
        identifier,session=str(uuid4()),str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);users.save_credential(runtime/'user-sessions',{'session_id':session,'token':'PRIVATE_SENTINEL'})
            response=MagicMock(status_code=200);response.json.return_value={'proposal':{'status':'APPROVED'}}
            output=StringIO()
            with patch('sys.argv',['user-proposals','--session',session,'decide',identifier,'--decision','APPROVED','--payload-hash','a'*64,'--version','0']),patch.object(user_proposals,'RUNTIME',runtime),patch('httpx.Client') as factory,redirect_stdout(output):
                factory.return_value.__enter__.return_value.request.return_value=response
                self.assertEqual(user_proposals.main(),0)
            factory.assert_called_once_with(trust_env=False,follow_redirects=False,timeout=10)
            request=factory.return_value.__enter__.return_value.request
            request.assert_called_once()
            self.assertEqual(request.call_args.args,('POST','http://127.0.0.1:8005/api/proposals/'+identifier+'/decision'))
            self.assertEqual(request.call_args.kwargs['json'],{'decision':'APPROVED','payload_hash':'a'*64,'expected_version':0})
            self.assertNotIn('PRIVATE_SENTINEL',output.getvalue())

    def test_reviewer_grant_change_after_authentication_stops_the_decision(self):
        svc=AuthenticatedProposals.__new__(AuthenticatedProposals);svc.proposals=MagicMock();svc.directory=MagicMock()
        reviewer=fixture_identity('approver')
        svc.directory.resolve.return_value=replace(reviewer,roles=frozenset({'reader'}))
        with self.assertRaises(ContractError) as denied:svc.decide(str(uuid4()),reviewer,SimpleNamespace(decision='APPROVED'))
        self.assertEqual(denied.exception.code,'FORBIDDEN')
        svc.proposals.decide.assert_not_called()

    def test_fixture_write_worker_cannot_consume_reserved_user_ledger(self):
        operations=SimpleNamespace(runs=SimpleNamespace(schema='harness_users_lab'))
        with self.assertRaises(ValueError):WriteWorker(operations,fixture_identity('operator'))
