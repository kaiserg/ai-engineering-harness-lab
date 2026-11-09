import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import unittest
from uuid import uuid4

from psycopg import sql

from harness.composition import compose, demo_identity
from harness.contracts import ContractError, Run, RunState
from harness.core import Limits
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.worker import Worker

ROOT = Path(__file__).parents[1]


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'harness_it_' + uuid4().hex
        self.store = PostgresRunStore(local_dsn(), schema=self.schema)
        self.identity = demo_identity()

    async def asyncTearDown(self):
        assert self.schema.startswith('harness_it_') and len(self.schema) == 43
        with self.store._database() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def enqueue(self, profile='fake-jira-v1', pause='none'):
        return self.store.enqueue(Run(self.identity.workspace_id, self.identity.actor_id,
            execution_profile=profile, limits_snapshot=asdict(Limits())), self.identity, pause_at=pause)

    async def test_two_consumers_cannot_claim_same_active_job(self):
        run = self.enqueue()
        barrier = Barrier(2)
        def claim(owner):
            barrier.wait(timeout=5)
            return self.store.claim_next(owner, self.identity)
        claims = await asyncio.gather(asyncio.to_thread(claim, 'one'), asyncio.to_thread(claim, 'two'))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        state = self.store.job_state(run.run_id, self.identity)
        self.assertEqual((state['status'], state['attempts'], state['fencing_token']), ('RUNNING', 1, 1))

    async def test_two_consumers_can_claim_distinct_jobs(self):
        first, second = self.enqueue(), self.enqueue()
        a, b = await asyncio.gather(asyncio.to_thread(self.store.claim_next, 'one', self.identity),
                                    asyncio.to_thread(self.store.claim_next, 'two', self.identity))
        self.assertEqual({a.run_id, b.run_id}, {first.run_id, second.run_id})

    async def test_expired_lease_cannot_be_renewed_and_new_attempt_increments_fence(self):
        run = self.enqueue()
        old = self.store.claim_next('same-worker-id', self.identity, lease_seconds=0.15)
        await asyncio.sleep(0.2)
        with self.assertRaises(ContractError) as error:
            self.store.renew(old, self.identity)
        self.assertEqual(error.exception.code, 'LEASE_LOST')
        fresh = self.store.claim_next('same-worker-id', self.identity)
        self.assertEqual(fresh.fencing_token, old.fencing_token + 1)
        current = self.store.load(run.run_id, self.identity)
        current.status = RunState.RUNNING
        with self.assertRaises(ContractError) as error:
            self.store.bind(old).save(current, self.identity)
        self.assertEqual(error.exception.code, 'LEASE_LOST')
        with self.assertRaises(ContractError):
            self.store.assert_claim(old, self.identity)
        self.store.bind(fresh).save(current, self.identity)

    async def test_heartbeat_keeps_a_long_await_owned(self):
        def slow_compose(*args, **kwargs):
            return compose(*args, **kwargs, delay=0.35)
        run = self.enqueue()
        task = asyncio.create_task(Worker(self.store, lease_seconds=0.3, composer=slow_compose).run_once())
        await asyncio.sleep(0.5)
        self.assertIsNone(self.store.claim_next('intruder', self.identity))
        result = await task
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(self.store.job_state(run.run_id, self.identity)['attempts'], 1)

    async def test_terminal_run_and_job_are_committed_together(self):
        run = self.enqueue()
        result = await Worker(self.store).run_once()
        saved, job = self.store.snapshot(run.run_id, self.identity)
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual((saved.status, job['status']), ('COMPLETED', 'DONE'))
        self.assertIsNone(job['owner'])
        self.assertIsNone(job['lease_until'])
        self.assertIsNone(self.store.claim_next('another', self.identity))

    async def test_pause_requeue_preserves_evidence_and_does_not_repeat_jira(self):
        run = self.enqueue(pause='jira')
        result = await Worker(self.store).run_once()
        before = self.store.load(run.run_id, self.identity)
        self.assertEqual(result['event'], 'paused')
        self.assertEqual(self.store.job_state(run.run_id, self.identity)['status'], 'PAUSED')
        self.assertIsNone(self.store.claim_next('other', self.identity))
        self.store.requeue_job(run.run_id, self.identity)
        result = await Worker(self.store).run_once()
        after = self.store.load(run.run_id, self.identity)
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(after.results, before.results)
        self.assertEqual(after.tool_calls, 1)
        self.assertEqual(self.store.job_state(run.run_id, self.identity)['attempts'], 2)

    async def test_cannot_requeue_a_live_lease_or_an_already_pending_job(self):
        run = self.enqueue()
        with self.assertRaises(ContractError):
            self.store.requeue_job(run.run_id, self.identity)
        self.store.claim_next('one', self.identity)
        with self.assertRaises(ContractError) as error:
            self.store.requeue_job(run.run_id, self.identity)
        self.assertEqual(error.exception.code, 'RUN_BUSY')

    async def test_worker_only_claims_its_trusted_actor_and_workspace(self):
        run = self.enqueue()
        for identity in (replace(self.identity, actor_id='other'), replace(self.identity, workspace_id='workspace-beta')):
            self.assertIsNone(self.store.claim_next('other', identity))
        self.assertIsNotNone(self.store.claim_next('correct', self.identity))

    async def test_queue_admission_limit_is_transactional(self):
        first = self.enqueue()
        another = Run(self.identity.workspace_id, self.identity.actor_id)
        with self.assertRaises(ContractError) as error:
            self.store.enqueue(another, self.identity, max_pending=1)
        self.assertEqual(error.exception.code, 'QUEUE_FULL')
        self.assertEqual([r.run_id for r in self.store.list(self.identity)], [first.run_id])

    async def test_claim_attempts_are_bounded_without_spending_model_budget(self):
        run = self.enqueue()
        with self.store._database() as db:
            db.execute('UPDATE jobs SET attempts = max_attempts WHERE run_id = %s', (run.run_id,))
        result = await Worker(self.store).run_once()
        saved = self.store.load(run.run_id, self.identity)
        self.assertEqual(result['error'], 'WORKER_ATTEMPTS_EXCEEDED')
        self.assertEqual((saved.status, saved.model_calls, saved.tool_calls), ('FAILED', 0, 0))
        self.assertEqual(self.store.job_state(run.run_id, self.identity)['status'], 'FAILED')

    async def test_worker_death_recovers_in_another_process_after_expiration(self):
        run = self.enqueue('mcp-devops-ci-v2:stale')
        command = [sys.executable, '-m', 'harness.worker', '--schema', self.schema, '--once']
        crashed = subprocess.run(command + ['--lease-seconds', '0.5', '--crash-at', 'after-result'],
                                 cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(crashed.returncode, 2, crashed.stderr)
        before = self.store.load(run.run_id, self.identity)
        self.assertEqual(len(before.results), 1)
        await asyncio.sleep(0.6)
        recovered = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        after = self.store.load(run.run_id, self.identity)
        self.assertEqual(after.assessment['status'], 'STALE_BUILD')
        self.assertEqual(after.results[0], before.results[0])
        self.assertEqual((after.model_calls, after.tool_calls), (4, 3))
        self.assertEqual(self.store.job_state(run.run_id, self.identity)['attempts'], 2)

    async def test_pending_read_after_process_death_preserves_call_id_and_charges_attempt(self):
        run = self.enqueue('mcp-devops-ci-v2:matching')
        command = [sys.executable, '-m', 'harness.worker', '--schema', self.schema, '--once']
        crashed = subprocess.run(command + ['--lease-seconds', '0.5', '--crash-at', 'before-tool', '--tool', 'jenkins_get_build'],
                                 cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(crashed.returncode, 2, crashed.stderr)
        before = self.store.load(run.run_id, self.identity)
        await asyncio.sleep(0.6)
        recovered = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        after = self.store.load(run.run_id, self.identity)
        self.assertEqual(after.status, 'COMPLETED')
        self.assertEqual(after.results[-1].call_id, before.pending_call.call_id)
        self.assertEqual(after.tool_calls, 4)
        self.assertGreaterEqual(after.recovered_time_seconds, before.pending_time_seconds)

    async def test_lost_lease_prevents_new_tool_dispatch_and_old_worker_commit(self):
        run = self.enqueue()
        entered = asyncio.Event()
        def stalled_compose(*args, **kwargs):
            execution = compose(*args, **kwargs)
            original = execution.harness.llm
            class StalledModel:
                async def decide(self, snapshot, tools):
                    entered.set()
                    await asyncio.sleep(1)
                    return await original.decide(snapshot, tools)
            execution.harness.llm = StalledModel()
            return execution
        task = asyncio.create_task(Worker(self.store, lease_seconds=0.6, composer=stalled_compose).run_once())
        await asyncio.wait_for(entered.wait(), 2)
        with self.store._database() as db:
            db.execute("UPDATE jobs SET lease_until = clock_timestamp() - interval '1 second' WHERE run_id = %s", (run.run_id,))
        newer = self.store.claim_next('replacement', self.identity)
        result = await task
        self.assertEqual(result['event'], 'lease_lost')
        saved = self.store.load(run.run_id, self.identity)
        self.assertEqual(saved.tool_calls, 0)
        self.assertEqual(self.store.job_state(run.run_id, self.identity)['fencing_token'], newer.fencing_token)
        self.assertNotEqual(saved.status, 'CANCELLED')

    async def test_current_permissions_are_checked_when_recovering_saved_results(self):
        run = self.enqueue(pause='jira')
        await Worker(self.store).run_once()
        self.store.requeue_job(run.run_id, self.identity)
        result = await Worker(self.store, identity=replace(self.identity, projects=frozenset())).run_once()
        self.assertEqual(result['error'], 'FORBIDDEN')
        self.assertEqual(self.store.job_state(run.run_id, self.identity)['status'], 'FAILED')

    async def test_bound_store_cannot_write_if_its_job_disappears(self):
        run = self.enqueue()
        claim = self.store.claim_next('one', self.identity)
        with self.store._database() as db:
            db.execute('DELETE FROM jobs WHERE job_id = %s', (claim.job_id,))
        run.status = RunState.RUNNING
        with self.assertRaises(ContractError) as error:
            self.store.bind(claim).save(run, self.identity)
        self.assertEqual(error.exception.code, 'LEASE_LOST')
