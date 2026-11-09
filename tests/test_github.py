import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from mcp import Client
from mcp.types import CallToolResult

from harness.adapters.mcp_github import MCPGitHub
from harness.contracts import ContractError, Identity, Run, ToolCall, pull_request_arguments
from harness.core import Harness, Limits
from harness.fakes import DevOpsScriptedLLM, FakeGitHub, FakeJira, ReadOnlyPolicy
from harness.mcp_contract import GITHUB_INPUT_SCHEMA
from harness.storage import SQLiteRunStore


PROFILE = 'mcp-devops-read-v1'


class GitHubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}),
                                 frozenset({'DEV'}), frozenset({'demo/atlas'}))
        self.call = ToolCall('github_get_pull_request', {'repository': 'demo/atlas', 'pull_number': 42})
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteRunStore(Path(self.directory.name) / 'runs.sqlite3')

    def harness(self, identity=None, limits=Limits(), hook=None):
        jira, github = FakeJira(include_pr=True), FakeGitHub()
        harness = Harness(DevOpsScriptedLLM(), ReadOnlyPolicy(),
            {'jira_get_issue': jira, 'github_get_pull_request': github}, self.store,
            limits=limits, checkpoint_hook=hook, execution_profile=PROFILE)
        return harness, jira, github

    def new_run(self):
        return self.store.create(Run('workspace-alpha', 'reader-demo', execution_profile=PROFILE), self.identity)

    async def test_real_github_mcp_contract_and_evidence(self):
        adapter = MCPGitHub(self.identity)
        result = await adapter.invoke(self.identity, self.call)
        self.assertEqual(result.call_id, self.call.call_id)
        self.assertEqual(result.tool_name, self.call.name)
        self.assertEqual(result.data['head_revision'], 'revision-A')
        self.assertEqual(result.source_id, 'github:demo/atlas:pr:42:revision-A')
        self.assertEqual((adapter.invocations, adapter.connections), (1, 1))

    async def test_jira_project_does_not_grant_repository_access_or_open_connection(self):
        identity = replace(self.identity, repositories=frozenset())
        adapter = MCPGitHub(identity, client_factory=lambda *_: self.fail('No conectar'))
        self.assertNotIn(self.call.name, ReadOnlyPolicy().visible_tools(identity))
        with self.assertRaises(ContractError) as error:
            await adapter.invoke(identity, self.call)
        self.assertEqual(error.exception.code, 'FORBIDDEN')
        self.assertEqual(adapter.connections, 0)
        ReadOnlyPolicy().authorize(identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}))

    async def test_server_rechecks_repository_and_workspace_without_host(self):
        for identity in (replace(self.identity, repositories=frozenset()),
                         replace(self.identity, workspace_id='workspace-beta')):
            async with Client(MCPGitHub(identity).parameters) as connection:
                listing = await connection.list_tools()
                self.assertEqual([tool.name for tool in listing.tools], [self.call.name])
                self.assertEqual(listing.tools[0].input_schema, GITHUB_INPUT_SCHEMA)
                result = await connection.session.call_tool(self.call.name, self.call.arguments)
                self.assertTrue(result.is_error)
                self.assertIn(result.structured_content['error']['code'], ('FORBIDDEN', 'NOT_FOUND'))

    async def test_argument_contract_rejects_urls_extra_fields_boolean_and_paths(self):
        for arguments in ({'repository': 'https://example.com/demo/atlas', 'pull_number': 42},
                          {'repository': '../atlas', 'pull_number': 42},
                          {'repository': 'demo/atlas', 'pull_number': True},
                          {'repository': 'demo/atlas', 'pull_number': 0},
                          {**self.call.arguments, 'role': 'admin'}):
            with self.assertRaises(ContractError):
                pull_request_arguments(arguments)
        async with Client(MCPGitHub(self.identity).parameters) as connection:
            await connection.list_tools()
            invalid = await connection.session.call_tool(self.call.name, {**self.call.arguments, 'role': 'admin'})
            self.assertTrue(invalid.is_error)

    async def test_wrong_resource_revision_or_error_output_is_rejected(self):
        payload = {'schema_version': 1, 'data': {**self.call.arguments, 'state': 'OPEN', 'head_revision': 'revision-A'},
                   'source_id': 'github:demo/atlas:pr:42:revision-A'}
        for field, value in [('repository', 'other/repo'), ('pull_number', 43),
                             ('pull_number', True), ('head_revision', 'revision-B')]:
            wrong = {**payload, 'data': {**payload['data'], field: value}}
            with self.assertRaises(ContractError) as error:
                MCPGitHub._normalize(CallToolResult(content=[], structuredContent=wrong), self.call)
            self.assertEqual(error.exception.code, 'INVALID_TOOL_OUTPUT')
        with self.assertRaises(ContractError) as error:
            MCPGitHub._normalize(CallToolResult(content=[], isError=True,
                                 structuredContent={'error': {'code': 'secret text'}}), self.call)
        self.assertEqual(error.exception.code, 'MCP_TOOL_ERROR')

    async def test_github_schema_change_does_not_dispatch(self):
        connection, context = AsyncMock(), AsyncMock()
        context.__aenter__.return_value = connection
        connection.list_tools.return_value = SimpleNamespace(tools=[SimpleNamespace(
            name=self.call.name, input_schema={**GITHUB_INPUT_SCHEMA, 'additionalProperties': True})])
        with self.assertRaises(ContractError) as error:
            await MCPGitHub(self.identity, client_factory=lambda *_: context).invoke(self.identity, self.call)
        self.assertEqual(error.exception.code, 'MCP_SCHEMA_MISMATCH')
        connection.session.call_tool.assert_not_called()

    async def test_two_tools_share_model_and_tool_budgets(self):
        for limits in (Limits(tool_calls=1), Limits(model_calls=2)):
            harness, jira, github = self.harness(limits=limits)
            result = await harness.execute(self.new_run().run_id, self.identity)
            self.assertEqual(result.outcome_code, 'BUDGET_EXCEEDED')
            self.assertEqual(jira.invocations, 1)
            self.assertLessEqual(result.tool_calls, limits.tool_calls)
            self.assertLessEqual(result.model_calls, limits.model_calls)

    async def test_revoked_repository_access_blocks_saved_github_result(self):
        harness, _, _ = self.harness()
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.status, 'COMPLETED')
        revoked = replace(self.identity, repositories=frozenset())
        with self.assertRaises(ContractError) as error:
            await harness.resume(result.run_id, revoked)
        self.assertEqual(error.exception.code, 'FORBIDDEN')

    async def test_pending_github_read_keeps_call_id_and_charges_new_attempt(self):
        def interrupt(point, run):
            if point == 'before-tool' and run.pending_call.name == self.call.name:
                raise SystemExit(2)
        harness, _, _ = self.harness(hook=interrupt)
        run = self.new_run()
        with self.assertRaises(SystemExit):
            await harness.execute(run.run_id, self.identity)
        pending = self.store.load(run.run_id, self.identity)
        self.assertEqual(pending.tool_calls, 2)
        harness, jira, github = self.harness()
        result = await harness.resume(run.run_id, self.identity)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(result.results[1].call_id, pending.pending_call.call_id)
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual((jira.invocations, github.invocations), (0, 1))
        self.assertGreaterEqual(result.recovered_time_seconds, pending.pending_time_seconds)

    async def test_github_timeout_retains_jira_evidence(self):
        harness, jira, github = self.harness(limits=Limits(tool_timeout=0.02))
        github.delay_seconds = 0.1
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UPSTREAM_TIMEOUT')
        self.assertEqual([r.tool_name for r in result.results], ['jira_get_issue'])
        self.assertEqual(result.pending_time_seconds, 0)

    async def test_global_deadline_still_spans_both_tools(self):
        harness, jira, github = self.harness(limits=Limits(active_timeout=0.05, tool_timeout=1))
        github.delay_seconds = 0.2
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'RUN_DEADLINE_EXCEEDED')
        self.assertEqual((jira.invocations, github.invocations), (1, 1))
        self.assertEqual(len(result.results), 1)
        self.assertGreaterEqual(result.active_seconds, 0.05)

    async def test_cancellation_in_github_keeps_confirmed_jira(self):
        harness, _, github = self.harness()
        entered = asyncio.Event()
        async def wait_forever(identity, call):
            entered.set()
            await asyncio.Event().wait()
        github.invoke = wait_forever
        run = self.new_run()
        task = asyncio.create_task(harness.execute(run.run_id, self.identity))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = self.store.load(run.run_id, self.identity)
        self.assertEqual(saved.status, 'CANCELLED')
        self.assertEqual(len(saved.results), 1)
        self.assertEqual(saved.pending_time_seconds, 0)


class GitHubCLITests(unittest.TestCase):
    def test_recovery_between_real_mcp_processes_skips_confirmed_tools(self):
        for count in (1, 2):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                command = [sys.executable, '-m', 'harness', '--db', str(Path(directory) / 'run.sqlite3')]
                cwd = Path(__file__).parents[1]
                cut = subprocess.run(command + ['--tool-backend', 'mcp', '--workflow', 'jira-github',
                    'start', '--interrupt-at', 'after-result', '--interrupt-after-results', str(count)],
                    cwd=cwd, capture_output=True, text=True)
                self.assertEqual(cut.returncode, 2, cut.stderr)
                before = json.loads(cut.stdout)
                result = json.loads(subprocess.check_output(command + ['resume', before['run_id']], cwd=cwd, text=True))
                self.assertEqual(result['status'], 'COMPLETED')
                self.assertEqual(result['execution_profile'], PROFILE)
                self.assertEqual((result['model_calls'], result['tool_calls']), (3, 2))
                self.assertEqual(result['tools_this_process']['jira']['connections'], 0)
                self.assertEqual(result['tools_this_process']['github']['connections'], 2 - count)
                self.assertIn('CI todavía no consultado', result['answer'])

    def test_pending_workflow_cannot_be_changed_to_jira_only(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, '-m', 'harness', '--db', str(Path(directory) / 'run.sqlite3')]
            cwd = Path(__file__).parents[1]
            cut = subprocess.run(command + ['--tool-backend', 'mcp', '--workflow', 'jira-github',
                'start', '--interrupt-at', 'after-result'], cwd=cwd, capture_output=True, text=True)
            self.assertEqual(cut.returncode, 2, cut.stderr)
            wrong = subprocess.run(command + ['--workflow', 'jira', 'resume', json.loads(cut.stdout)['run_id']],
                                   cwd=cwd, capture_output=True, text=True)
            self.assertEqual(json.loads(wrong.stdout)['error'], 'CONFIG_CHANGED')
