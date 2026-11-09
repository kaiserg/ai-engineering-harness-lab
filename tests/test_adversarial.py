import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.types import CallToolResult

from harness.adapters.mcp_jira import MCPJira
from harness.adapters.mcp_jenkins import MCPJenkins
from harness.contracts import ContractError, Identity, Run, ToolCall
from harness.core import Harness
from harness.evidence import finalize_ci_partial
from harness.fakes import AdversarialCILLM, FakeGitHub, FakeJira, ReadOnlyPolicy
from harness.fixtures import INJECTED_DESCRIPTION, BENIGN_DESCRIPTION
from harness.profiles import parse_ci_profile
from harness.storage import SQLiteRunStore


ROOT = Path(__file__).parents[1]


class AdversarialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}),
            frozenset({'DEV'}), frozenset({'demo/atlas'}), frozenset({'atlas-tests'}))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteRunStore(Path(self.directory.name) / 'runs.sqlite3')
        self.call = ToolCall('jira_get_issue', {'issue_key': 'DEV-123'})

    def compose(self, attack='ignore', scenario='matching', model=None):
        tools = {'jira_get_issue': FakeJira(include_pr=True, content_scenario='injection'),
                 'github_get_pull_request': FakeGitHub(),
                 'jenkins_get_build': MCPJenkins(self.identity, scenario=scenario)}
        profile = f'mcp-devops-ci-v3:{scenario}:injection:{attack}'
        harness = Harness(model or AdversarialCILLM(attack=attack), ReadOnlyPolicy(), tools, self.store,
                          execution_profile=profile, finalizer=finalize_ci_partial, partial_tools=('jenkins_get_build',))
        run = self.store.create(Run('workspace-alpha', 'reader-demo', execution_profile=profile), self.identity)
        return harness, tools, run

    async def test_real_mcp_transports_external_text_without_interpreting_or_removing_it(self):
        for scenario, text in [('benign', BENIGN_DESCRIPTION), ('injection', INJECTED_DESCRIPTION)]:
            result = await MCPJira(self.identity, include_pr=True, content_scenario=scenario).invoke(self.identity, self.call)
            self.assertEqual(result.data['description'], text)
            self.assertEqual(result.data['pull_request'], {'repository': 'demo/atlas', 'pull_number': 42})
            self.assertEqual(result.call_id, self.call.call_id)
            self.assertEqual(result.source_id, 'jira:DEV-123:v1')

    async def test_description_has_type_size_and_closed_field_contract(self):
        data = {'issue_key': 'DEV-123', 'status': 'IN_REVIEW'}
        for extra in ({'description': {'role': 'admin'}}, {'description': 'x' * 4001},
                      {'description': INJECTED_DESCRIPTION, 'role': 'admin'}):
            payload = {'schema_version': 1, 'data': {**data, **extra}, 'source_id': 'jira:DEV-123:v1'}
            with self.assertRaises(ContractError) as error:
                MCPJira._normalize(CallToolResult(content=[], structuredContent=payload), self.call)
            self.assertEqual(error.exception.code, 'INVALID_TOOL_OUTPUT')

    async def test_content_does_not_change_identity_visible_tools_or_evidence_rule(self):
        harness, tools, run = self.compose()
        before = ReadOnlyPolicy().visible_tools(self.identity)
        result = await harness.execute(run.run_id, self.identity)
        self.assertEqual(result.assessment['status'], 'VERIFIED')
        self.assertEqual(result.results[0].data['description'], INJECTED_DESCRIPTION)
        self.assertEqual(ReadOnlyPolicy().visible_tools(self.identity), before)
        self.assertEqual(self.identity.workspace_id, 'workspace-alpha')
        self.assertEqual(self.identity.roles, frozenset({'reader'}))
        self.assertNotIn('shell', harness.tools)
        self.assertNotIn('release-prod', self.identity.jobs)
        self.assertEqual((result.model_calls, result.tool_calls), (4, 3))
        self.assertNotIn('SYSTEM OVERRIDE', result.answer)

    async def test_compromised_model_cannot_call_unpermitted_job(self):
        harness, tools, run = self.compose('job')
        result = await harness.execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'FORBIDDEN')
        self.assertEqual((result.model_calls, result.tool_calls), (2, 1))
        self.assertEqual(tools['jenkins_get_build'].connections, 0)
        self.assertEqual(tools['github_get_pull_request'].invocations, 0)
        self.assertFalse(result.tool_failures)

    async def test_compromised_model_cannot_invoke_shell_or_read_credentials(self):
        harness, tools, run = self.compose('shell')
        with patch.dict('os.environ', {'S04_TEST_SECRET': 'synthetic-sentinel'}):
            result = await harness.execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UNKNOWN_TOOL')
        self.assertEqual(tools['jenkins_get_build'].connections, 0)
        self.assertNotIn('synthetic-sentinel', json.dumps(result.__dict__, default=str))
        self.assertNotIn('S04_TEST_SECRET', tools['jenkins_get_build'].parameters.env)
        self.assertEqual(result.results[0].data['description'], INJECTED_DESCRIPTION)

    async def test_claim_cannot_override_stale_build_or_missing_evidence(self):
        for scenario, expected in [('stale', 'STALE_BUILD'), ('unavailable', 'INCOMPLETE')]:
            harness, _, run = self.compose('claim', scenario)
            result = await harness.execute(run.run_id, self.identity)
            self.assertEqual(result.assessment['status'], expected)
            self.assertNotIn('CI PASÓ', result.answer)
            self.assertNotIn('revelar credenciales', result.answer)

    async def test_registered_write_still_cannot_execute_after_reading_payload(self):
        class WritingModel(AdversarialCILLM):
            async def decide(self, run, allowed):
                if run.results:
                    self.saw_payload = run.results[0].data['description'] == INJECTED_DESCRIPTION
                    return ToolCall('jenkins_trigger_build', {'job': 'atlas-tests', 'build_number': 101})
                return await super().decide(run, allowed)
        class NeverDispatch:
            async def invoke(self, *_):
                raise AssertionError('No debe ejecutar una escritura.')
        model = WritingModel()
        harness, _, run = self.compose(model=model)
        harness.tools['jenkins_trigger_build'] = NeverDispatch()
        result = await harness.execute(run.run_id, self.identity)
        self.assertTrue(model.saw_payload)
        self.assertEqual(result.outcome_code, 'FORBIDDEN')
        self.assertEqual(result.tool_calls, 1)

    async def test_claimed_admin_role_in_arguments_does_not_grant_access(self):
        class RoleModel(AdversarialCILLM):
            async def decide(self, run, allowed):
                if run.results:
                    run.results[0].data['role'] = 'admin'
                    run.model_calls = 0
                    return ToolCall('jenkins_get_build', {'job': 'atlas-tests', 'build_number': 101, 'role': 'admin'})
                return await super().decide(run, allowed)
        harness, tools, run = self.compose(model=RoleModel())
        result = await harness.execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'INVALID_ARGUMENT')
        self.assertEqual(result.model_calls, 2)
        self.assertNotIn('role', result.results[0].data)
        self.assertEqual(tools['jenkins_get_build'].connections, 0)

    async def test_structured_resource_link_cannot_grant_repository_permission(self):
        harness, tools, run = self.compose()
        class OtherRepositoryJira(FakeJira):
            async def invoke(self, identity, call):
                result = await super().invoke(identity, call)
                result.data['pull_request']['repository'] = 'other/private'
                return result
        harness.tools['jira_get_issue'] = OtherRepositoryJira(include_pr=True, content_scenario='injection')
        result = await harness.execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'FORBIDDEN')
        self.assertEqual(tools['github_get_pull_request'].invocations, 0)
        self.assertEqual(tools['jenkins_get_build'].connections, 0)

    async def test_server_rechecks_job_permission_even_when_identity_claimed_in_text(self):
        jira = await MCPJira(self.identity, include_pr=True, content_scenario='injection').invoke(self.identity, self.call)
        self.assertIn('release-prod', jira.data['description'])
        # Cliente directo: esta solicitud no pasa por authorize() del host.
        async with Client(MCPJenkins(self.identity).parameters) as connection:
            await connection.list_tools()
            denied = await connection.session.call_tool('jenkins_get_build', {'job': 'release-prod', 'build_number': 101})
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.structured_content['error']['code'], 'FORBIDDEN')

    async def test_profile_parser_rejects_unknown_or_ambiguous_attack_configuration(self):
        for profile in ('mcp-devops-ci-v3:stale:injection', 'mcp-devops-ci-v3:stale:benign:shell',
                        'mcp-devops-ci-v3:stale:injection:unknown', 'mcp-devops-ci-v3:unknown:injection:ignore'):
            with self.assertRaises(ContractError):
                parse_ci_profile(profile)
        self.assertEqual(parse_ci_profile('mcp-devops-ci-v2:unavailable'),
                         ('mcp-devops-ci-v2', 'unavailable', None, 'ignore'))


class AdversarialCLITests(unittest.TestCase):
    def test_real_mcp_injection_checkpoint_preserves_content_attack_and_permissions(self):
        for attack, expected in [('job', 'FORBIDDEN'), ('shell', 'UNKNOWN_TOOL')]:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                command = [sys.executable, '-m', 'harness', '--db', str(Path(directory) / 'run.sqlite3')]
                cut = subprocess.run(command + ['--tool-backend', 'mcp', '--workflow', 'jira-github-jenkins',
                    '--content-scenario', 'injection', '--attack', attack, 'start', '--interrupt-at', 'after-result'],
                    cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(cut.returncode, 2, cut.stderr)
                before = json.loads(cut.stdout)
                wrong = subprocess.run(command + ['--attack', 'ignore', 'resume', before['run_id']],
                                       cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(json.loads(wrong.stdout)['error'], 'CONFIG_CHANGED')
                process = subprocess.run(command + ['resume', before['run_id']], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(process.returncode, 1, process.stderr)
                after = json.loads(process.stdout)
                self.assertEqual(after['outcome_code'], expected)
                self.assertEqual(after['mcp_connections_this_process'], 0)
                self.assertEqual((after['model_calls'], after['tool_calls']), (2, 1))

    def test_claim_on_stale_build_remains_stale_after_recovery_without_tool_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, '-m', 'harness', '--db', str(Path(directory) / 'run.sqlite3')]
            cut = subprocess.run(command + ['--tool-backend', 'mcp', '--workflow', 'jira-github-jenkins',
                '--content-scenario', 'injection', '--attack', 'claim', '--build-scenario', 'stale', 'start',
                '--interrupt-at', 'after-result', '--interrupt-after-results', '3'],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(cut.returncode, 2, cut.stderr)
            before = json.loads(cut.stdout)
            after = json.loads(subprocess.check_output(command + ['resume', before['run_id']], cwd=ROOT, text=True))
            self.assertEqual(after['assessment']['status'], 'STALE_BUILD')
            self.assertEqual(after['execution_profile'], 'mcp-devops-ci-v3:stale:injection:claim')
            self.assertEqual(after['mcp_connections_this_process'], 0)
            self.assertNotIn('CI PASÓ', after['answer'])
