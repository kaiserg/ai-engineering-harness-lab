"""Ledger PostgreSQL + servidor MCP real + efecto SQLite en otro proceso."""
import asyncio
import unittest
from lease_wait import wait_for_expired_lease
from unittest.mock import patch
import test_operations as fixtures
from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite
from harness.contracts import ContractError
from harness.operations import Operations,OperationInterrupted


class OperationMCPTests(unittest.IsolatedAsyncioTestCase):
    code=fixtures.OperationTests.code
    decide=fixtures.OperationTests.decide
    approved=fixtures.OperationTests.approved
    asyncTearDown=fixtures.OperationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.OperationTests.asyncSetUp(self)
        self.backend=self.provider
        self.provider=MCPJenkinsWrite(self.operator,self.backend.path)
        self.ops=Operations(self.store,self.provider,self.principals)

    def effects(self):
        with self.backend._db() as db:return db.execute('SELECT count(*) AS n FROM requests').fetchone()['n']

    async def test_mcp_dispatch_and_repeat_in_separate_servers_produces_one_effect(self):
        row=self.approved();identifier=row['operation_id']
        first=await asyncio.to_thread(self.ops.dispatch,identifier,self.operator)
        self.assertEqual(first['status'],'SUCCEEDED')
        reopened=Operations(self.store,MCPJenkinsWrite(self.operator,self.backend.path),self.principals)
        self.assertEqual(reopened.dispatch(identifier,self.operator),first)
        self.assertEqual(self.effects(),1)
        self.assertEqual(self.provider.lookup(self.operator,identifier,self.item['payload_hash']),first['receipt'])

    async def test_server_death_after_commit_recovers_without_sending_again(self):
        row=self.approved();identifier=row['operation_id']
        self.provider.parameters.env['HARNESS_JENKINS_WRITE_FAULT']='exit-after-commit'
        unknown=self.ops.dispatch(identifier,self.operator)
        self.assertEqual((unknown['status'],unknown['outcome_code']),('UNKNOWN','UNKNOWN_EFFECT'))
        self.assertEqual(self.effects(),1)
        self.code('UNSAFE_RECOVERY',lambda:self.ops.dispatch(identifier,self.operator))
        reopened=Operations(self.store,MCPJenkinsWrite(self.operator,self.backend.path),self.principals)
        with patch.object(reopened.provider,'request',side_effect=AssertionError('do not resend')):
            result=reopened.reconcile(identifier,self.operator)
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(self.effects(),1)
        self.assertEqual([e['status'] for e in reopened.get(identifier,self.operator)['events']],
            ['PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED'])

    async def test_known_rejection_stays_not_applied_over_mcp(self):
        row=self.approved();self.backend.set_revision(self.operator.workspace_id,'revision-B')
        result=self.ops.dispatch(row['operation_id'],self.operator)
        self.assertEqual((result['status'],result['outcome_code']),('NOT_APPLIED','STALE_PRECONDITION'))
        self.assertEqual(self.effects(),0)

    async def test_missing_server_at_reconcile_preserves_unknown(self):
        row=self.approved();identifier=row['operation_id']
        with patch.object(self.provider,'request',side_effect=ContractError('MCP_UNAVAILABLE')):
            self.ops.dispatch(identifier,self.operator)
        with patch.object(self.provider,'lookup',side_effect=ContractError('MCP_UNAVAILABLE')):
            self.code('MCP_UNAVAILABLE',lambda:self.ops.reconcile(identifier,self.operator))
        self.assertEqual(self.ops.get(identifier,self.operator)['status'],'UNKNOWN')
        self.assertEqual(self.effects(),0)

    async def test_lookup_after_host_interruption_preserves_original_lease(self):
        row=self.approved();identifier=row['operation_id']
        with self.assertRaises(OperationInterrupted):self.ops.dispatch(identifier,self.operator,interrupt='after-send')
        self.code('OPERATION_BUSY',lambda:self.ops.reconcile(identifier,self.operator))
        await asyncio.to_thread(wait_for_expired_lease,self.runs,identifier)
        result=self.ops.reconcile(identifier,self.operator)
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(self.effects(),1)
