import asyncio
from contextlib import closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from harness.contracts import ContractError, FinalAnswer, Identity, Run, RunState, ToolCall, ToolResult
from harness.core import Harness, Limits
from harness.fakes import FakeJira, ReadOnlyPolicy, ScriptedLLM
from harness.storage import SQLiteRunStore


class Clock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, amount):
        self.value += amount


class TimingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'runs.sqlite3'
        self.store = SQLiteRunStore(self.path)
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}), frozenset({'DEV'}))

    def new(self):
        return self.store.create(Run('workspace-alpha', 'reader-demo'), self.identity)

    def core(self, llm=None, tool=None, limits=Limits(), clock=None, hook=None):
        return Harness(llm or ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': tool or FakeJira()},
                       self.store, limits, checkpoint_hook=hook, clock=clock or Clock())

    async def test_global_budget_spans_multiple_individually_valid_calls(self):
        clock = Clock()
        async def decide(snapshot, _):
            clock.advance(0.4 if not snapshot.results else 0.3)
            return ToolCall('jira_get_issue', {'issue_key': 'DEV-123'}) if not snapshot.results else FinalAnswer('done')
        async def invoke(identity, call):
            clock.advance(0.4)
            return ToolResult(call.call_id, {'issue_key': 'DEV-123'}, 'jira:DEV-123:v1')
        llm, tool = AsyncMock(), AsyncMock()
        llm.decide.side_effect, tool.invoke.side_effect = decide, invoke
        run = self.new()
        result = await self.core(llm, tool, Limits(active_timeout=1), clock).execute(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'RUN_DEADLINE_EXCEEDED')
        self.assertEqual(result.status, RunState.FAILED)
        self.assertAlmostEqual(result.active_seconds, 1.1)
        self.assertEqual(result.pending_time_seconds, 0)
        self.assertIsNone(result.answer)
        self.assertAlmostEqual(self.store.load(run.run_id, self.identity).active_seconds, 1.1)

    async def test_actual_await_is_capped_by_remaining_global_time(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def wait(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        llm = AsyncMock()
        llm.decide.side_effect = wait
        # Reloj real: el global de 50 ms gana al timeout individual de 30 s.
        core = Harness(llm, ReadOnlyPolicy(), {'jira_get_issue': FakeJira()}, self.store,
                       Limits(active_timeout=0.05))
        result = await core.execute(self.new().run_id, self.identity)
        self.assertTrue(entered.is_set() and cancelled.is_set())
        self.assertEqual(result.outcome_code, 'RUN_DEADLINE_EXCEEDED')
        self.assertEqual(result.tool_calls, 0)
        self.assertGreaterEqual(result.active_seconds, 0.05)

    async def test_individual_timeout_still_reports_upstream(self):
        llm = AsyncMock()
        llm.decide.side_effect = TimeoutError('private details')
        result = await self.core(llm).execute(self.new().run_id, self.identity)
        self.assertEqual(result.outcome_code, 'UPSTREAM_TIMEOUT')
        self.assertEqual(result.pending_time_seconds, 0)

    async def test_persisted_time_survives_restart_without_charging_downtime(self):
        clock = Clock()
        async def decide(*_):
            clock.advance(0.2)
            return ToolCall('jira_get_issue', {'issue_key': 'DEV-123'})
        async def invoke(identity, call):
            clock.advance(0.3)
            return ToolResult(call.call_id, {'issue_key': 'DEV-123'}, 'jira:DEV-123:v1')
        def crash(point, snapshot):
            if point == 'after-result':
                raise SystemExit(2)
        llm, tool = AsyncMock(), AsyncMock()
        llm.decide.side_effect, tool.invoke.side_effect = decide, invoke
        run = self.new()
        with self.assertRaises(SystemExit):
            await self.core(llm, tool, clock=clock, hook=crash).execute(run.run_id, self.identity)
        before = SQLiteRunStore(self.path).load(run.run_id, self.identity)
        self.assertAlmostEqual(before.active_seconds, 0.5)
        self.assertEqual(before.pending_time_seconds, 0)
        later = Clock(10000)  # Otro origen monotónico y mucho tiempo de proceso detenido.
        async def finish(*_):
            later.advance(0.1)
            return FinalAnswer('done')
        model = AsyncMock()
        model.decide.side_effect = finish
        jira = FakeJira()
        result = await self.core(model, jira, clock=later).resume(run.run_id, self.identity)
        self.assertAlmostEqual(result.active_seconds, 0.6)
        self.assertEqual(result.recovered_time_seconds, 0)
        self.assertEqual(jira.invocations, 0)

    async def test_orphan_reservation_is_charged_and_expired_run_does_not_call_model(self):
        limits = Limits(active_timeout=1, model_timeout=1)
        def crash(point, snapshot):
            if point == 'before-model':
                raise SystemExit(2)
        run = self.new()
        with self.assertRaises(SystemExit):
            await self.core(limits=limits, hook=crash).execute(run.run_id, self.identity)
        before = self.store.load(run.run_id, self.identity)
        self.assertEqual(before.pending_time_seconds, 1)
        model = AsyncMock()
        result = await self.core(model, limits=limits, clock=Clock(5000)).resume(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'RUN_DEADLINE_EXCEEDED')
        self.assertEqual(result.active_seconds, 1)
        self.assertEqual(result.recovered_time_seconds, 1)
        self.assertEqual(result.pending_time_seconds, 0)
        model.decide.assert_not_called()

    async def test_partial_orphan_reservation_leaves_only_remainder(self):
        limits = Limits(active_timeout=1, model_timeout=0.4)
        run = self.new()
        def crash(point, snapshot):
            if point == 'before-model':
                raise SystemExit(2)
        with self.assertRaises(SystemExit):
            await self.core(limits=limits, hook=crash).execute(run.run_id, self.identity)
        clock = Clock(99)
        async def finish(*_):
            clock.advance(0.1)
            return FinalAnswer('done')
        model = AsyncMock()
        model.decide.side_effect = finish
        result = await self.core(model, limits=limits, clock=clock).resume(run.run_id, self.identity)
        self.assertEqual(result.status, RunState.COMPLETED)
        self.assertAlmostEqual(result.active_seconds, 0.5)
        self.assertEqual(result.recovered_time_seconds, 0.4)
        cached = await self.core(model, limits=limits, clock=Clock(99999)).resume(run.run_id, self.identity)
        self.assertEqual(cached.active_seconds, result.active_seconds)
        self.assertEqual(model.decide.await_count, 1)

    async def test_cancel_records_elapsed_time_without_exception_details(self):
        clock = Clock()
        async def cancel(*_):
            clock.advance(0.3)
            raise asyncio.CancelledError()
        llm = AsyncMock()
        llm.decide.side_effect = cancel
        run = self.new()
        with self.assertRaises(asyncio.CancelledError):
            await self.core(llm, clock=clock).execute(run.run_id, self.identity)
        saved = self.store.load(run.run_id, self.identity)
        self.assertEqual(saved.status, RunState.CANCELLED)
        self.assertEqual(saved.pending_time_seconds, 0)
        self.assertEqual(saved.active_seconds, 0.3)

    async def test_legacy_run_without_time_is_readable_but_not_resumable(self):
        run = self.new()
        payload = asdict(run)
        payload['schema_version'] = 1
        payload['status'] = 'RUNNING'
        for name in ('active_seconds', 'pending_time_seconds', 'recovered_time_seconds', 'timing_version'):
            del payload[name]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE runs SET payload = ? WHERE run_id = ?', (json.dumps(payload), run.run_id))
        legacy = self.store.load(run.run_id, self.identity)
        self.assertEqual(legacy.timing_version, 0)
        command = [sys.executable, '-m', 'harness', '--db', str(self.path), 'status', run.run_id]
        output = json.loads(subprocess.check_output(command, cwd=Path(__file__).parents[1], text=True))
        self.assertIsNone(output['active_seconds'])
        self.assertIsNone(output['active_remaining_seconds'])
        with self.assertRaises(ContractError) as error:
            await self.core().resume(run.run_id, self.identity)
        self.assertEqual(error.exception.code, 'TIME_BUDGET_UNKNOWN')
        self.assertEqual(self.store.load(run.run_id, self.identity).status, RunState.RUNNING)

    async def test_time_cannot_decrease_or_be_invalid(self):
        run = self.new()
        run.status, run.active_seconds = RunState.RUNNING, 0.5
        saved = self.store.save(run, self.identity)
        for invalid in (replace(saved, active_seconds=0.4), replace(saved, active_seconds=-1),
                        replace(saved, active_seconds=float('nan')),
                        replace(saved, pending_time_seconds=float('inf'))):
            with self.assertRaises(ContractError):
                self.store.save(invalid, self.identity)
        self.assertEqual(self.store.load(run.run_id, self.identity).active_seconds, 0.5)

    async def test_limits_reject_nonfinite_zero_negative_and_boolean(self):
        for value in (0, -1, float('nan'), float('inf'), True, '10'):
            with self.assertRaises(ValueError):
                Limits(active_timeout=value)


class TimingCLITests(unittest.TestCase):
    def test_cli_uses_saved_budget_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            command = [sys.executable, '-m', 'harness', '--db', str(path)]
            cwd = Path(__file__).parents[1]
            first = subprocess.run(command + ['--active-timeout', '0.1', 'start', '--interrupt-at', 'before-model'],
                                   cwd=cwd, capture_output=True, text=True)
            self.assertEqual(first.returncode, 2, first.stderr)
            before = json.loads(first.stdout)
            resumed = subprocess.run(command + ['resume', before['run_id']], cwd=cwd,
                                     capture_output=True, text=True)
            self.assertEqual(resumed.returncode, 1, resumed.stderr)
            after = json.loads(resumed.stdout)
            self.assertEqual(after['active_limit_seconds'], 0.1)
            self.assertEqual(after['outcome_code'], 'RUN_DEADLINE_EXCEEDED')
            self.assertEqual(after['active_remaining_seconds'], 0)
            self.assertGreater(after['recovered_time_seconds'], 0)
