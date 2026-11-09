import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from mcp import Client
from mcp.types import CallToolResult

from harness.adapters.mcp_jira import MCPJira
from harness.contracts import ContractError, Identity, Run, RunState, ToolCall
from harness.core import Harness, Limits
from harness.fakes import ReadOnlyPolicy, ScriptedLLM
from harness.mcp_contract import JIRA_INPUT_SCHEMA
from harness.storage import SQLiteRunStore


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}), frozenset({'DEV'}))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteRunStore(Path(self.directory.name) / 'runs.sqlite3')

    async def test_real_mcp_client_discovers_schema_and_normalizes_result(self):
        adapter = MCPJira(self.identity)
        call = ToolCall('jira_get_issue', {'issue_key': 'DEV-123'})
        result = await adapter.invoke(self.identity, call)
        self.assertEqual(result.call_id, call.call_id)
        self.assertEqual(result.source_id, 'jira:DEV-123:v1')
        self.assertEqual(result.data['status'], 'IN_REVIEW')
        self.assertEqual((adapter.connections, adapter.invocations), (1, 1))

    async def test_server_denies_direct_client_and_extra_fields_without_host_policy(self):
        adapter = MCPJira(self.identity)
        async with Client(adapter.parameters) as connection:
            listing = await connection.list_tools()
            self.assertEqual([tool.name for tool in listing.tools], ['jira_get_issue'])
            self.assertEqual(listing.tools[0].input_schema, JIRA_INPUT_SCHEMA)
            denied = await connection.session.call_tool('jira_get_issue', {'issue_key': 'OTHER-1'})
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.structured_content['error']['code'], 'FORBIDDEN')
            invalid = await connection.session.call_tool('jira_get_issue', {'issue_key': 'DEV-123', 'role': 'admin'})
            self.assertTrue(invalid.is_error)
            missing = await connection.session.call_tool('jira_get_issue', {'issue_key': 'DEV-999'})
            self.assertTrue(missing.is_error)
            self.assertEqual(missing.structured_content['error']['code'], 'NOT_FOUND')

    async def test_identity_bound_in_server_not_taken_from_tool_arguments(self):
        for identity in (replace(self.identity, roles=frozenset()),
                         replace(self.identity, workspace_id='workspace-beta')):
            adapter = MCPJira(identity)
            async with Client(adapter.parameters) as connection:
                await connection.list_tools()
                result = await connection.session.call_tool('jira_get_issue', {'issue_key': 'DEV-123'})
                self.assertTrue(result.is_error)

    async def test_host_denial_does_not_open_session(self):
        def forbidden_factory(*_):
            self.fail('No debe abrir el cliente.')
        adapter = MCPJira(self.identity, client_factory=forbidden_factory)
        for identity, call in [
            (self.identity, ToolCall('jira_delete_issue', {'issue_key': 'DEV-123'})),
            (replace(self.identity, workspace_id='workspace-beta'), ToolCall('jira_get_issue', {'issue_key': 'DEV-123'})),
            (self.identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123', 'role': 'admin'})),
        ]:
            with self.assertRaises(ContractError):
                await adapter.invoke(identity, call)
        self.assertEqual((adapter.connections, adapter.invocations), (0, 0))

    async def test_schema_change_is_not_automatically_accepted(self):
        connection = AsyncMock()
        connection.list_tools.return_value = SimpleNamespace(tools=[SimpleNamespace(
            name='jira_get_issue', input_schema={**JIRA_INPUT_SCHEMA, 'additionalProperties': True})])
        context = AsyncMock()
        context.__aenter__.return_value = connection
        adapter = MCPJira(self.identity, client_factory=lambda *_: context)
        with self.assertRaises(ContractError) as error:
            await adapter.invoke(self.identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}))
        self.assertEqual(error.exception.code, 'MCP_SCHEMA_MISMATCH')
        connection.session.call_tool.assert_not_called()

    async def test_rpc_failures_are_sanitized(self):
        context = AsyncMock()
        context.__aenter__.side_effect = RuntimeError('PRIVATE credentials')
        adapter = MCPJira(self.identity, client_factory=lambda *_: context)
        with self.assertRaises(ContractError) as error:
            await adapter.invoke(self.identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}))
        self.assertEqual(str(error.exception), 'MCP_UNAVAILABLE')

    async def test_malformed_and_wrong_resource_outputs_rejected(self):
        call = ToolCall('jira_get_issue', {'issue_key': 'DEV-123'})
        for payload in (None, {'schema_version': 1},
                        {'schema_version': 1, 'data': {'issue_key': 'DEV-999', 'status': 'IN_REVIEW'}, 'source_id': 'jira:DEV-999:v1'}):
            result = CallToolResult(content=[], structuredContent=payload)
            with self.assertRaises(ContractError) as error:
                MCPJira._normalize(result, call)
            self.assertEqual(error.exception.code, 'INVALID_TOOL_OUTPUT')
        result = CallToolResult(content=[], isError=True, structuredContent={'error': 'PRIVATE'})
        with self.assertRaises(ContractError) as error:
            MCPJira._normalize(result, call)
        self.assertEqual(error.exception.code, 'MCP_TOOL_ERROR')

    async def test_global_deadline_includes_connection_startup(self):
        cancelled = asyncio.Event()
        context = AsyncMock()
        async def never_connect():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        context.__aenter__.side_effect = never_connect
        adapter = MCPJira(self.identity, client_factory=lambda *_: context)
        run = self.store.create(Run('workspace-alpha', 'reader-demo', execution_profile='mcp-jira-fixture-v1'), self.identity)
        result = await Harness(ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': adapter}, self.store,
                               Limits(active_timeout=0.05), execution_profile=run.execution_profile).execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'RUN_DEADLINE_EXCEEDED')
        self.assertTrue(cancelled.is_set())
        self.assertEqual(adapter.invocations, 0)
        self.assertEqual(result.pending_time_seconds, 0)

    async def test_real_mcp_slow_tool_respects_individual_timeout(self):
        adapter = MCPJira(self.identity, delay_seconds=2)
        run = self.store.create(Run('workspace-alpha', 'reader-demo', execution_profile='mcp-jira-fixture-v1'), self.identity)
        result = await Harness(ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': adapter}, self.store,
                               Limits(tool_timeout=1), execution_profile=run.execution_profile).execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UPSTREAM_TIMEOUT')
        self.assertEqual(adapter.connections, 1)
        self.assertEqual(result.pending_time_seconds, 0)


class MCPCLITests(unittest.TestCase):
    def test_checkpoint_resume_uses_saved_profile_without_new_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'runs.sqlite3'
            command = [sys.executable, '-m', 'harness', '--db', str(db)]
            cwd = Path(__file__).parents[1]
            cut = subprocess.run(command + ['--tool-backend', 'mcp', 'start', '--interrupt-at', 'after-result'],
                                 cwd=cwd, capture_output=True, text=True)
            self.assertEqual(cut.returncode, 2, cut.stderr)
            before = json.loads(cut.stdout)
            self.assertEqual(before['mcp_connections_this_process'], 1)
            after = json.loads(subprocess.check_output(command + ['resume', before['run_id']], cwd=cwd, text=True))
            self.assertEqual(after['status'], 'COMPLETED')
            self.assertEqual(after['execution_profile'], 'mcp-jira-fixture-v1')
            self.assertEqual(after['tool_calls'], 1)
            self.assertEqual(after['mcp_connections_this_process'], 0)
            self.assertEqual(after['adapter_invocations_this_process'], 0)

    def test_profile_change_is_rejected_before_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, '-m', 'harness', '--db', str(Path(directory) / 'run.sqlite3')]
            cwd = Path(__file__).parents[1]
            cut = subprocess.run(command + ['--tool-backend', 'mcp', 'start', '--interrupt-at', 'before-tool'],
                                 cwd=cwd, capture_output=True, text=True)
            self.assertEqual(cut.returncode, 2, cut.stderr)
            before = json.loads(cut.stdout)
            wrong = subprocess.run(command + ['--tool-backend', 'fake', 'resume', before['run_id']],
                                   cwd=cwd, capture_output=True, text=True)
            self.assertEqual(wrong.returncode, 1)
            self.assertEqual(json.loads(wrong.stdout)['error'], 'CONFIG_CHANGED')
