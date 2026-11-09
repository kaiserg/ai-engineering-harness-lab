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

from harness.contracts import ContractError, Identity, Run, RunState, ToolCall
from harness.core import Harness, Limits
from harness.fakes import FakeJira, MemoryRunStore, ReadOnlyPolicy, ScriptedLLM
from harness.storage import SQLiteRunStore

ROOT = Path(__file__).parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'runs.sqlite3'
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}), frozenset({'DEV'}))

    def test_memory_and_sqlite_share_contract_guards(self):
        for store in (MemoryRunStore(), SQLiteRunStore(self.path)):
            with self.subTest(store=type(store).__name__):
                run = store.create(Run('workspace-alpha', 'reader-demo'), self.identity)
                with self.assertRaises(ContractError):
                    store.create(run, self.identity)
                for identity in (replace(self.identity, workspace_id='workspace-beta'),
                                 replace(self.identity, actor_id='another-reader')):
                    with self.assertRaises(ContractError) as error:
                        store.load(run.run_id, identity)
                    self.assertEqual(error.exception.code, 'NOT_FOUND')
                stale = store.load(run.run_id, self.identity)
                run.status = RunState.RUNNING
                run = store.save(run, self.identity)
                with self.assertRaises(ContractError) as error:
                    store.save(stale, self.identity)
                self.assertEqual(error.exception.code, 'CONFLICT')
                run.status = RunState.QUEUED
                with self.assertRaises(ContractError):
                    store.save(run, self.identity)  # Solo el camino explícito de recuperación.
                run = store.requeue_read(run, self.identity)
                self.assertEqual(run.status, RunState.QUEUED)
                run.status = RunState.RUNNING
                run = store.save(run, self.identity)
                run.status = RunState.COMPLETED
                run = store.save(run, self.identity)
                with self.assertRaises(ContractError):
                    store.save(run, self.identity)

    def test_two_store_instances_do_not_overwrite_newer_version(self):
        first, second = SQLiteRunStore(self.path), SQLiteRunStore(self.path)
        run = first.create(Run('workspace-alpha', 'reader-demo'), self.identity)
        stale = second.load(run.run_id, self.identity)
        run.status = RunState.RUNNING
        saved = first.save(run, self.identity)
        stale.status = RunState.RUNNING
        with self.assertRaises(ContractError) as error:
            second.save(stale, self.identity)
        self.assertEqual(error.exception.code, 'CONFLICT')
        self.assertEqual(second.load(run.run_id, self.identity), saved)

    def test_cannot_reduce_budget_or_change_config(self):
        store = SQLiteRunStore(self.path)
        run = store.create(Run('workspace-alpha', 'reader-demo'), self.identity)
        run.status, run.model_calls = RunState.RUNNING, 1
        run.limits_snapshot = asdict(Limits())
        run = store.save(run, self.identity)
        for invalid in (replace(run, model_calls=0), replace(run, limits_snapshot={'model_calls': 99})):
            with self.assertRaises(ContractError):
                store.save(invalid, self.identity)
        self.assertEqual(store.load(run.run_id, self.identity).model_calls, 1)

    def test_bad_schema_and_corrupt_payload_rejected(self):
        store = SQLiteRunStore(self.path)
        run = store.create(Run('workspace-alpha', 'reader-demo'), self.identity)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE runs SET payload = ? WHERE run_id = ?', ('not json', run.run_id))
        with self.assertRaises(ContractError) as error:
            store.load(run.run_id, self.identity)
        self.assertEqual(error.exception.code, 'CORRUPT_STATE')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA user_version = 99')
        with self.assertRaises(ContractError) as error:
            SQLiteRunStore(self.path)
        self.assertEqual(error.exception.code, 'STORAGE_SCHEMA_UNSUPPORTED')

    def test_cli_recovery_across_processes(self):
        for point in ('after-result', 'before-tool', 'before-model'):
            with self.subTest(point=point):
                path = self.path.with_name(point + '.sqlite3')
                command = [sys.executable, '-m', 'harness', '--db', str(path)]
                crashed = subprocess.run(command + ['start', '--interrupt-at', point],
                                         cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(crashed.returncode, 2, crashed.stderr)
                before = json.loads(crashed.stdout)
                self.assertEqual(before['status'], 'RUNNING')
                status = json.loads(subprocess.check_output(command + ['status', before['run_id']],
                                                           cwd=ROOT, text=True))
                self.assertEqual(status['checkpoint'], before['checkpoint'])
                recovered = subprocess.run(command + ['resume', before['run_id']],
                                           cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(recovered.returncode, 0, recovered.stderr + recovered.stdout)
                after = json.loads(recovered.stdout)
                self.assertEqual(after['run_id'], before['run_id'])
                self.assertEqual(after['status'], 'COMPLETED')
                self.assertEqual(after['saved_results'], 1)
                self.assertEqual(after['tool_calls'], 2 if point == 'before-tool' else 1)
                self.assertEqual(after['model_calls'], 3 if point == 'before-model' else 2)
                self.assertEqual(after['adapter_invocations_this_process'], 0 if point == 'after-result' else 1)
                repeated = json.loads(subprocess.check_output(command + ['resume', before['run_id']],
                                                             cwd=ROOT, text=True))
                self.assertEqual(repeated['adapter_invocations_this_process'], 0)
                self.assertEqual(repeated['version'], after['version'])


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteRunStore(Path(self.directory.name) / 'run.sqlite3')
        self.identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}), frozenset({'DEV'}))

    async def interrupted(self, point='after-result', limits=Limits()):
        run = self.store.create(Run('workspace-alpha', 'reader-demo'), self.identity)
        def crash(where, snapshot):
            if where == point:
                raise SystemExit(2)
        core = Harness(ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': FakeJira()},
                       self.store, limits, checkpoint_hook=crash)
        with self.assertRaises(SystemExit):
            await core.execute(run.run_id, self.identity)
        return self.store.load(run.run_id, self.identity)

    async def test_pending_call_keeps_correlation_and_reserved_budget(self):
        run = await self.interrupted('before-tool')
        jira = FakeJira()
        result = await Harness(ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': jira}, self.store).resume(
            run.run_id, self.identity)
        self.assertEqual(result.results[0].call_id, run.pending_call.call_id)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(jira.invocations, 1)

    async def test_resume_does_not_reset_model_budget(self):
        limits = Limits(model_calls=1)
        run = await self.interrupted(limits=limits)
        llm = AsyncMock()
        result = await Harness(llm, ReadOnlyPolicy(), {'jira_get_issue': FakeJira()}, self.store, limits).resume(
            run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'BUDGET_EXCEEDED')
        self.assertEqual(result.model_calls, 1)
        llm.decide.assert_not_called()

    async def test_revoked_access_and_changed_limits_do_not_resume(self):
        run = await self.interrupted()
        llm = AsyncMock()
        core = Harness(llm, ReadOnlyPolicy(), {'jira_get_issue': FakeJira()}, self.store)
        with self.assertRaises(ContractError) as error:
            await core.resume(run.run_id, replace(self.identity, projects=frozenset()))
        self.assertEqual(error.exception.code, 'FORBIDDEN')
        with self.assertRaises(ContractError) as error:
            await Harness(llm, ReadOnlyPolicy(), {}, self.store, Limits(model_calls=99)).resume(
                run.run_id, self.identity)
        self.assertEqual(error.exception.code, 'CONFIG_CHANGED')
        self.assertEqual(self.store.load(run.run_id, self.identity).status, RunState.RUNNING)
        llm.decide.assert_not_called()

    async def test_pending_write_is_not_replayed(self):
        run = self.store.create(Run('workspace-alpha', 'reader-demo'), self.identity)
        run.status, run.checkpoint = RunState.RUNNING, 'TOOL_PENDING'
        run.pending_call = ToolCall('jenkins_request_tests', {'revision': 'revision-A'})
        run = self.store.save(run, self.identity)
        tool, llm = AsyncMock(), AsyncMock()
        with self.assertRaises(ContractError) as error:
            await Harness(llm, ReadOnlyPolicy(), {'jenkins_request_tests': tool}, self.store).resume(
                run.run_id, self.identity)
        self.assertEqual(error.exception.code, 'UNSAFE_RECOVERY')
        tool.invoke.assert_not_called()
        llm.decide.assert_not_called()

    async def test_pending_read_cannot_bypass_tool_budget_on_resume(self):
        limits = Limits(tool_calls=1)
        run = await self.interrupted('before-tool', limits=limits)
        jira = FakeJira()
        result = await Harness(ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': jira},
                               self.store, limits).resume(run.run_id, self.identity)
        self.assertEqual(result.outcome_code, 'BUDGET_EXCEEDED')
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(jira.invocations, 0)
