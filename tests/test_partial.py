import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness.contracts import ContractError, FinalAnswer, Identity, Run, ToolFailure
from harness.core import Harness, Limits
from harness.evidence import finalize_ci_partial
from harness.fakes import CIScriptedLLM, FakeGitHub, FakeJira, ReadOnlyPolicy
from harness.storage import SQLiteRunStore, decode, encode


ROOT = Path(__file__).parents[1]
PROFILE = 'mcp-devops-ci-v2:unavailable'


class FailingRead:
    def __init__(self, code='UPSTREAM_UNAVAILABLE', delay=0):
        self.code, self.delay, self.invocations = code, delay, 0

    async def invoke(self, identity, call):
        self.invocations += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.code == 'unexpected':
            raise RuntimeError('PRIVATE DETAILS')
        raise ContractError(self.code)


class PartialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}),
            frozenset({'DEV'}), frozenset({'demo/atlas'}), frozenset({'atlas-tests'}))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteRunStore(Path(self.directory.name) / 'runs.sqlite3')

    def compose(self, code='UPSTREAM_UNAVAILABLE', delay=0, hook=None, limits=Limits(), model=None):
        tools = {'jira_get_issue': FakeJira(include_pr=True), 'github_get_pull_request': FakeGitHub(),
                 'jenkins_get_build': FailingRead(code, delay)}
        harness = Harness(model or CIScriptedLLM(), ReadOnlyPolicy(), tools, self.store,
            execution_profile=PROFILE, finalizer=finalize_ci_partial, partial_tools=('jenkins_get_build',),
            checkpoint_hook=hook, limits=limits)
        return harness, tools

    def new_run(self):
        return self.store.create(Run('workspace-alpha', 'reader-demo', execution_profile=PROFILE), self.identity)

    async def test_optional_failure_is_not_a_build_result_or_false_negative(self):
        harness, tools = self.compose()
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(result.assessment['status'], 'INCOMPLETE')
        self.assertIsNone(result.assessment['same_revision'])
        self.assertIsNone(result.assessment['build_successful'])
        self.assertIsNone(result.assessment['build_revision'])
        self.assertEqual(len(result.results), 2)
        self.assertEqual(len(result.tool_failures), 1)
        self.assertEqual((result.model_calls, result.tool_calls), (4, 3))
        self.assertEqual(tools['jenkins_get_build'].invocations, 1)
        self.assertEqual(result.pending_time_seconds, 0)
        self.assertIsNone(result.pending_call)
        self.assertIn('No podemos confirmar si CI pasó', result.answer)
        self.assertNotIn('[jenkins:', result.answer)
        saved = self.store.load(result.run_id, self.identity)
        self.assertEqual(saved.tool_failures, result.tool_failures)

    async def test_permission_contract_and_ambiguous_transport_errors_still_block(self):
        for code in ('FORBIDDEN', 'INVALID_ARGUMENT', 'INVALID_TOOL_OUTPUT', 'MCP_SCHEMA_MISMATCH',
                     'MCP_UNAVAILABLE', 'MCP_TOOL_ERROR', 'NOT_FOUND', 'BUDGET_EXCEEDED', 'RUN_DEADLINE_EXCEEDED'):
            with self.subTest(code=code):
                harness, _ = self.compose(code)
                result = await harness.execute(self.new_run().run_id, self.identity)
                self.assertEqual(result.status, 'FAILED')
                self.assertEqual(result.outcome_code, code)
                self.assertFalse(result.tool_failures)
                self.assertIsNone(result.assessment)

    async def test_unexpected_error_remains_sanitized_and_blocking(self):
        harness, _ = self.compose('unexpected')
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'INTERNAL_ERROR')
        self.assertNotIn('PRIVATE', encode(result))
        self.assertFalse(result.tool_failures)

    async def test_individual_timeout_can_produce_partial_report(self):
        harness, _ = self.compose(delay=0.1, limits=Limits(tool_timeout=0.02))
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.assessment['status'], 'INCOMPLETE')
        self.assertEqual(result.tool_failures[0].code, 'UPSTREAM_TIMEOUT')
        self.assertGreaterEqual(result.active_seconds, 0.02)
        self.assertEqual(result.pending_time_seconds, 0)

    async def test_global_deadline_wins_over_optional_failure(self):
        harness, _ = self.compose(delay=0.1, limits=Limits(active_timeout=0.02))
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'RUN_DEADLINE_EXCEEDED')
        self.assertIsNone(result.assessment)
        self.assertFalse(result.tool_failures)

    async def test_error_in_required_read_is_not_degraded(self):
        harness, tools = self.compose()
        harness.tools['jira_get_issue'] = FailingRead()
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UPSTREAM_UNAVAILABLE')
        self.assertFalse(result.results)
        self.assertFalse(result.tool_failures)
        self.assertEqual(tools['jenkins_get_build'].invocations, 0)

    async def test_error_in_model_is_not_a_tool_failure(self):
        class BrokenModel:
            async def decide(self, run, allowed):
                raise ContractError('UPSTREAM_UNAVAILABLE')
        harness, _ = self.compose(model=BrokenModel())
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UPSTREAM_UNAVAILABLE')
        self.assertFalse(result.tool_failures)

    async def test_model_cannot_erase_missing_evidence_or_claim_ci_passed(self):
        class OptimisticModel(CIScriptedLLM):
            async def decide(self, run, allowed):
                if run.tool_failures:
                    run.tool_failures.clear()  # El modelo solo recibe una copia.
                    return FinalAnswer('CI PASÓ: desplegar.')
                return await super().decide(run, allowed)
        harness, _ = self.compose(model=OptimisticModel())
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.assessment['status'], 'INCOMPLETE')
        self.assertEqual(len(result.tool_failures), 1)
        self.assertNotIn('CI PASÓ', result.answer)

    async def test_recovery_of_saved_failure_does_not_repeat_tools_or_reset_counts(self):
        def interrupt(point, run):
            if point == 'after-tool-failure':
                raise SystemExit(2)
        harness, _ = self.compose(hook=interrupt)
        run = self.new_run()
        with self.assertRaises(SystemExit):
            await harness.execute(run.run_id, self.identity)
        before = self.store.load(run.run_id, self.identity)
        self.assertEqual(before.checkpoint, 'READY')
        self.assertEqual((before.model_calls, before.tool_calls), (3, 3))
        harness, tools = self.compose()
        result = await harness.resume(run.run_id, self.identity)
        self.assertEqual(result.assessment['status'], 'INCOMPLETE')
        self.assertEqual((result.model_calls, result.tool_calls), (4, 3))
        self.assertEqual(sum(tool.invocations for tool in tools.values()), 0)
        self.assertEqual(result.tool_failures[0].call.call_id, before.tool_failures[0].call.call_id)
        self.assertEqual(result.recovered_time_seconds, 0)

    async def test_revoked_job_blocks_returning_a_partial_report(self):
        harness, _ = self.compose()
        result = await harness.execute(self.new_run().run_id, self.identity)
        with self.assertRaises(ContractError) as error:
            await harness.resume(result.run_id, replace(self.identity, jobs=frozenset()))
        self.assertEqual(error.exception.code, 'FORBIDDEN')

    async def test_stored_failure_cannot_be_deleted_or_changed(self):
        def interrupt(point, run):
            if point == 'after-tool-failure':
                raise SystemExit(2)
        harness, _ = self.compose(hook=interrupt)
        run = self.new_run()
        with self.assertRaises(SystemExit):
            await harness.execute(run.run_id, self.identity)
        for change in ('delete', 'change'):
            saved = self.store.load(run.run_id, self.identity)
            if change == 'delete':
                saved.tool_failures.clear()
            else:
                saved.tool_failures[0] = replace(saved.tool_failures[0], code='UPSTREAM_TIMEOUT')
            with self.assertRaises(ContractError) as error:
                self.store.save(saved, self.identity)
            self.assertEqual(error.exception.code, 'INVALID_CHECKPOINT')

    async def test_partial_report_still_requires_remaining_model_and_time_budget(self):
        harness, _ = self.compose(limits=Limits(model_calls=3))
        result = await harness.execute(self.new_run().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'BUDGET_EXCEEDED')
        self.assertEqual(len(result.tool_failures), 1)
        self.assertIsNone(result.assessment)

    async def test_cancellation_during_optional_read_is_not_a_partial_success(self):
        harness, _ = self.compose()
        entered = asyncio.Event()
        class WaitingTool:
            async def invoke(self, identity, call):
                entered.set()
                await asyncio.Event().wait()
        harness.tools['jenkins_get_build'] = WaitingTool()
        run = self.new_run()
        task = asyncio.create_task(harness.execute(run.run_id, self.identity))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = self.store.load(run.run_id, self.identity)
        self.assertEqual(saved.status, 'CANCELLED')
        self.assertFalse(saved.tool_failures)
        self.assertIsNone(saved.assessment)

    async def test_old_payload_has_no_invented_failures(self):
        run = self.new_run()
        payload = json.loads(encode(run))
        payload.pop('tool_failures')
        restored = decode(json.dumps(payload))
        self.assertEqual(restored.tool_failures, [])
        self.assertIsNone(restored.assessment)


class PartialCLITests(unittest.TestCase):
    def test_real_go_failure_checkpoint_recovers_without_any_mcp_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, '-m', 'harness', '--db', str(Path(directory) / 'run.sqlite3')]
            cut = subprocess.run(command + ['--tool-backend', 'mcp', '--workflow', 'jira-github-jenkins',
                '--build-scenario', 'unavailable', 'start', '--interrupt-at', 'after-tool-failure'],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(cut.returncode, 2, cut.stderr)
            before = json.loads(cut.stdout)
            self.assertEqual(before['execution_profile'], PROFILE)
            self.assertEqual(len(before['tool_failures']), 1)
            result = json.loads(subprocess.check_output(command + ['resume', before['run_id']], cwd=ROOT, text=True))
            self.assertEqual(result['status'], 'COMPLETED')
            self.assertEqual(result['assessment']['status'], 'INCOMPLETE')
            self.assertEqual(result['mcp_connections_this_process'], 0)
            self.assertEqual(result['adapter_invocations_this_process'], 0)
            self.assertEqual((result['model_calls'], result['tool_calls']), (4, 3))

    def test_previous_profile_keeps_fail_fast_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'run.sqlite3'
            identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}),
                frozenset({'DEV'}), frozenset({'demo/atlas'}), frozenset({'atlas-tests'}))
            run = SQLiteRunStore(db).create(Run('workspace-alpha', 'reader-demo',
                execution_profile='mcp-devops-ci-v1:unavailable'), identity)
            process = subprocess.run([sys.executable, '-m', 'harness', '--db', str(db), 'resume', run.run_id],
                                     cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(process.returncode, 1, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result['execution_profile'], 'mcp-devops-ci-v1:unavailable')
            self.assertEqual(result['outcome_code'], 'UPSTREAM_UNAVAILABLE')
            self.assertEqual(result['tool_failures'], [])
            self.assertIsNone(result['assessment'])
