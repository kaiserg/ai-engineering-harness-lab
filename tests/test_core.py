import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

from harness.contracts import (ContractError, FinalAnswer, Identity, Run, RunState,
                               ToolCall, ToolResult)
from harness.core import Harness, Limits
from harness.fakes import FakeJira, MemoryRunStore, ReadOnlyPolicy, ScriptedLLM


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}), frozenset({'DEV'}))
        self.store, self.jira = MemoryRunStore(), FakeJira()
        self.run = self.store.create(Run(self.identity.workspace_id, self.identity.actor_id), self.identity)

    def core(self, llm=None, limits=Limits(), tools=None):
        return Harness(llm or ScriptedLLM(), ReadOnlyPolicy(), tools or {'jira_get_issue': self.jira},
                       self.store, limits)

    async def test_success_records_evidence_counts_and_final_state(self):
        result = await self.core().execute(self.run.run_id, self.identity)
        self.assertEqual(result.status, RunState.COMPLETED)
        self.assertEqual((result.model_calls, result.tool_calls, self.jira.invocations), (2, 1, 1))
        self.assertIn('jira:DEV-123:v1', result.answer)
        self.assertEqual(self.store.load(result.run_id, self.identity).results, result.results)

    async def test_registered_but_prohibited_tool_never_reaches_adapter(self):
        result = await self.core(ScriptedLLM('jira_delete_issue'), tools={
            'jira_get_issue': self.jira, 'jira_delete_issue': self.jira,
        }).execute(self.run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'FORBIDDEN')
        self.assertEqual(self.jira.invocations, 0)

    async def test_unknown_tool_rejected_before_adapter(self):
        result = await self.core(ScriptedLLM('shell')).execute(self.run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UNKNOWN_TOOL')
        self.assertEqual(self.jira.invocations, 0)

    async def test_extra_fields_are_rejected(self):
        llm = AsyncMock()
        llm.decide.return_value = ToolCall('jira_get_issue', {'issue_key': 'DEV-123', 'role': 'admin'})
        result = await self.core(llm).execute(self.run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'INVALID_ARGUMENT')
        self.assertEqual(self.jira.invocations, 0)

    async def test_allowed_tools_are_filtered_and_model_cannot_mutate_run(self):
        async def decide(snapshot, allowed_tools):
            self.assertEqual(allowed_tools, ('jira_get_issue',))
            snapshot.model_calls = -100
            return FinalAnswer('Respuesta ficticia sin herramientas.')
        llm = AsyncMock()
        llm.decide.side_effect = decide
        result = await self.core(llm, tools={'jira_get_issue': self.jira, 'shell': self.jira}).execute(
            self.run.run_id, self.identity)
        self.assertEqual(result.model_calls, 1)

    async def test_cross_workspace_or_actor_does_not_call_model(self):
        for identity in (replace(self.identity, workspace_id='workspace-beta'),
                         replace(self.identity, actor_id='other-reader')):
            llm = AsyncMock()
            with self.assertRaises(ContractError) as error:
                await self.core(llm).execute(self.run.run_id, identity)
            self.assertEqual(error.exception.code, 'NOT_FOUND')
            llm.decide.assert_not_called()
        self.assertEqual(self.store.load(self.run.run_id, self.identity).status, RunState.QUEUED)

    async def test_project_permission_enforced_in_host_and_fake_tool(self):
        identity = replace(self.identity, projects=frozenset({'OTHER'}))
        result = await self.core().execute(self.run.run_id, identity)
        self.assertEqual(result.outcome_code, 'FORBIDDEN')
        with self.assertRaises(ContractError):
            await self.jira.invoke(identity, ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}))
        self.assertEqual(self.jira.invocations, 0)

    async def test_model_and_tool_budgets_stop_repeated_calls(self):
        for limits in (Limits(model_calls=1), Limits(tool_calls=1)):
            run = self.store.create(Run(self.identity.workspace_id, self.identity.actor_id), self.identity)
            llm = AsyncMock()
            llm.decide.side_effect = lambda *_: ToolCall('jira_get_issue', {'issue_key': 'DEV-123'})
            result = await self.core(llm, limits).execute(run.run_id, self.identity)
            self.assertEqual(result.outcome_code, 'BUDGET_EXCEEDED')
            self.assertEqual(result.tool_calls, 1)

    async def test_read_timeout_preserves_reserved_count(self):
        async def timeout(*_):
            raise TimeoutError('PRIVATE input from adapter')
        tool = AsyncMock()
        tool.invoke.side_effect = timeout
        result = await self.core(tools={'jira_get_issue': tool}).execute(self.run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UPSTREAM_TIMEOUT')
        self.assertEqual(result.tool_calls, 1)
        self.assertIsNone(result.answer)

    async def test_actual_tool_deadline_cancels_wait(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def wait(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        tool = AsyncMock()
        tool.invoke.side_effect = wait
        result = await self.core(limits=Limits(tool_timeout=0.05), tools={'jira_get_issue': tool}).execute(
            self.run.run_id, self.identity)
        self.assertTrue(started.is_set() and cancelled.is_set())
        self.assertEqual(result.outcome_code, 'UPSTREAM_TIMEOUT')

    async def test_cancel_marks_read_only_run_and_propagates_cancel(self):
        entered = asyncio.Event()
        async def wait(*_):
            entered.set()
            await asyncio.Event().wait()
        tool = AsyncMock()
        tool.invoke.side_effect = wait
        task = asyncio.create_task(self.core(tools={'jira_get_issue': tool}).execute(self.run.run_id, self.identity))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.load(self.run.run_id, self.identity).status, RunState.CANCELLED)

    async def test_invalid_result_and_internal_error_are_sanitized(self):
        for response in ('PRIVATE bad response', ToolResult('wrong-call', {}, 'source')):
            run = self.store.create(Run(self.identity.workspace_id, self.identity.actor_id), self.identity)
            tool = AsyncMock()
            tool.invoke.return_value = response
            result = await self.core(tools={'jira_get_issue': tool}).execute(run.run_id, self.identity)
            self.assertEqual(result.outcome_code, 'INVALID_TOOL_OUTPUT')
        tool = AsyncMock()
        tool.invoke.side_effect = RuntimeError('PRIVATE credentials')
        result = await self.core(tools={'jira_get_issue': tool}).execute(self.run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'INTERNAL_ERROR')
        self.assertNotIn('PRIVATE', str(result))

    async def test_completed_run_is_not_executed_twice(self):
        await self.core().execute(self.run.run_id, self.identity)
        with self.assertRaises(ContractError):
            await self.core().execute(self.run.run_id, self.identity)
        self.assertEqual(self.jira.invocations, 1)
