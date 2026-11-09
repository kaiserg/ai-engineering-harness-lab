"""Frontera MCP de escritura: schemas, scope, errores y respuestas no confiables."""
import asyncio
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4
from mcp import Client
from mcp.types import CallToolResult
from scripts.proposals import fixture_identity
from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite
from harness.contracts import ContractError
from harness.jenkins_simulator import JenkinsSimulator
from harness.jenkins_write_contract import validate


class WriteMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'provider.sqlite3'
        self.identity=fixture_identity('operator')
        self.client=MCPJenkinsWrite(self.identity,self.path)
        self.values={'operation_id':str(uuid4()),'payload_hash':'a'*64,
            'arguments':{'job':'atlas-tests','repository':'demo/atlas','revision':'revision-A'},
            'not_after':(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat()}

    def code(self,expected,callback):
        with self.assertRaises(ContractError) as error:callback()
        self.assertEqual(error.exception.code,expected)

    async def test_real_server_revalidates_identity_when_client_policy_is_bypassed(self):
        reader=replace(self.identity,roles=frozenset({'reader'}))
        adapter=MCPJenkinsWrite(reader,self.path)
        async with Client(adapter.parameters) as connection:
            result=await connection.session.call_tool('jenkins_request_tests',self.values)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content,{'error':{'code':'FORBIDDEN'}})
        with JenkinsSimulator(self.path)._db() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM requests').fetchone()['n'],0)

    async def test_server_rejects_extra_identity_or_arbitrary_job(self):
        async with Client(self.client.parameters) as connection:
            for changed in (dict(self.values,actor_id='admin'),dict(self.values,arguments={'job':'release-prod'})):
                result=await connection.session.call_tool('jenkins_request_tests',changed)
                self.assertTrue(result.is_error)
        self.code('INVALID_ARGUMENT',lambda:validate('jenkins_find_request',{'operation_id':True,'payload_hash':'a'*64}))

    async def test_adapter_rejects_different_identity_before_connection(self):
        self.code('FORBIDDEN',lambda:self.client.lookup(replace(self.identity,workspace_id='other'),self.values['operation_id'],'a'*64))
        self.assertFalse(self.path.exists())

    async def test_response_requires_exact_id_hash_receipt_and_schema_version(self):
        good={'schema_version':1,'data':{'operation_id':self.values['operation_id'],
            'payload_hash':self.values['payload_hash'],'receipt':'queue:'+self.values['operation_id']}}
        for changed in (dict(good,schema_version=True),dict(good,data=dict(good['data'],payload_hash='b'*64)),
                dict(good,data=dict(good['data'],operation_id=str(uuid4()))),dict(good,data=dict(good['data'],receipt=None))):
            result=CallToolResult(content=[],structuredContent=changed)
            self.code('INVALID_TOOL_OUTPUT',lambda:MCPJenkinsWrite.normalize('jenkins_request_tests',self.values,result))
        ambiguous=CallToolResult(content=[],structuredContent={'error':{'code':'DISPATCH_EXPIRED','extra':'unknown'}},isError=True)
        self.code('MCP_TOOL_ERROR',lambda:MCPJenkinsWrite.normalize('jenkins_request_tests',self.values,ambiguous))

    async def test_changed_discovery_schema_does_not_dispatch(self):
        session=SimpleNamespace(call_tool=AsyncMock())
        class FakeClient:
            def __init__(self,*args,**kwargs):self.session=session
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def list_tools(self):return SimpleNamespace(tools=[])
        client=MCPJenkinsWrite(self.identity,self.path,client_factory=FakeClient)
        self.code('MCP_SCHEMA_MISMATCH',lambda:client.revision(self.identity.workspace_id))
        session.call_tool.assert_not_called()

    async def test_timeout_includes_startup(self):
        class HangingClient:
            def __init__(self,*args,**kwargs):pass
            async def __aenter__(self):await asyncio.sleep(1)
            async def __aexit__(self,*args):pass
        client=MCPJenkinsWrite(self.identity,self.path,client_factory=HangingClient,timeout=.02)
        self.code('MCP_UNAVAILABLE',lambda:client.revision(self.identity.workspace_id))

    async def test_closure_proof_requires_positive_fence_and_bound_metadata(self):
        from harness.jenkins_write_contract import validate_closure
        proof={'operation_id':self.values['operation_id'],'payload_hash':self.values['payload_hash'],
            'receipt':None,'closed':True,'closed_at':datetime.now(timezone.utc).isoformat(),'evidence_id':'closure:'+'b'*64}
        self.assertEqual(validate_closure(proof,self.values['operation_id'],self.values['payload_hash']),proof)
        for invalid in (proof|{'closed':False},proof|{'closed':1},proof|{'receipt':'queue:other'},proof|{'closed_at':'no-date'},proof|{'payload_hash':'c'*64}):
            self.code('INVALID_TOOL_OUTPUT',lambda:validate_closure(invalid,self.values['operation_id'],self.values['payload_hash']))

    async def test_lost_provider_database_is_not_silently_recreated_as_evidence(self):
        JenkinsSimulator(self.path)
        self.path.unlink()
        self.code('SIMULATOR_STATE_LOST',lambda:JenkinsSimulator(self.path))
        self.assertFalse(self.path.exists())
