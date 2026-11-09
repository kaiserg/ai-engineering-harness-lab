"""Integración real: requiere scripts/postgres.py up; no omite pruebas si falta la base."""
import asyncio
from dataclasses import asdict, replace
import json
from threading import Barrier
import unittest
from uuid import uuid4

from psycopg import sql

from harness.composition import compose, demo_identity
from harness.contracts import ContractError, Run, RunState
from harness.core import Harness, Limits
from harness.fakes import FakeJira, ReadOnlyPolicy, ScriptedLLM
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore


class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'harness_it_' + uuid4().hex
        self.store = PostgresRunStore(local_dsn(), schema=self.schema)
        self.identity = demo_identity()

    async def asyncTearDown(self):
        # Únicamente el schema aleatorio creado para esta prueba, nunca harness_lab.
        assert self.schema.startswith('harness_it_') and len(self.schema) == 43
        with self.store._database() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def run_record(self, profile='fake-jira-v1'):
        return Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile,
                   limits_snapshot=asdict(Limits()))

    async def test_run_store_roundtrip_and_independent_connection(self):
        original = self.run_record()
        saved = self.store.create(original, self.identity)
        second = PostgresRunStore(local_dsn(), schema=self.schema)
        self.assertEqual(second.load(saved.run_id, self.identity), original)
        self.assertEqual(second.list(self.identity), [original])
        self.assertEqual(second.pending_jobs(self.identity), [])

    async def test_enqueue_commits_run_and_pending_job_together(self):
        run = self.store.enqueue(self.run_record(), self.identity)
        jobs = self.store.pending_jobs(self.identity)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['run_id'], run.run_id)
        self.assertEqual(jobs[0]['status'], 'PENDING')
        self.assertEqual(self.store.load(run.run_id, self.identity).status, 'QUEUED')

    async def test_failed_job_insert_rolls_back_new_run(self):
        job_id = str(uuid4())
        first = self.store.enqueue(self.run_record(), self.identity, job_id=job_id)
        second = self.run_record()
        with self.assertRaises(ContractError) as error:
            self.store.enqueue(second, self.identity, job_id=job_id)
        self.assertEqual(error.exception.code, 'CONFLICT')
        with self.assertRaises(ContractError) as missing:
            self.store.load(second.run_id, self.identity)
        self.assertEqual(missing.exception.code, 'NOT_FOUND')
        self.assertEqual([r.run_id for r in self.store.list(self.identity)], [first.run_id])
        self.assertEqual(len(self.store.pending_jobs(self.identity)), 1)

    async def test_duplicate_run_does_not_create_another_job(self):
        run = self.store.enqueue(self.run_record(), self.identity)
        with self.assertRaises(ContractError) as error:
            self.store.enqueue(run, self.identity)
        self.assertEqual(error.exception.code, 'CONFLICT')
        self.assertEqual(len(self.store.pending_jobs(self.identity)), 1)

    async def test_actor_and_workspace_filter_runs_and_jobs(self):
        run = self.store.enqueue(self.run_record(), self.identity)
        for identity in (replace(self.identity, actor_id='other-reader'),
                         replace(self.identity, workspace_id='workspace-beta')):
            with self.assertRaises(ContractError) as error:
                self.store.load(run.run_id, identity)
            self.assertEqual(error.exception.code, 'NOT_FOUND')
            self.assertEqual(self.store.list(identity), [])
            self.assertEqual(self.store.pending_jobs(identity), [])

    async def test_concurrent_stale_writers_cannot_both_commit(self):
        run = self.store.create(self.run_record(), self.identity)
        second = PostgresRunStore(local_dsn(), schema=self.schema)
        first_copy, second_copy = self.store.load(run.run_id, self.identity), second.load(run.run_id, self.identity)
        first_copy.status = second_copy.status = RunState.RUNNING
        barrier = Barrier(2)
        def save(store, candidate):
            barrier.wait(timeout=5)
            try:
                return store.save(candidate, self.identity).version
            except ContractError as error:
                return error.code
        answers = await asyncio.gather(asyncio.to_thread(save, self.store, first_copy),
                                       asyncio.to_thread(save, second, second_copy))
        self.assertCountEqual(answers, [1, 'CONFLICT'])
        self.assertEqual(self.store.load(run.run_id, self.identity).version, 1)

    async def test_counters_timing_and_configuration_remain_immutable(self):
        run = self.store.create(self.run_record(), self.identity)
        run.status, run.model_calls, run.active_seconds = RunState.RUNNING, 1, 0.1
        run = self.store.save(run, self.identity)
        for invalid in (replace(run, model_calls=0), replace(run, active_seconds=0),
                        replace(run, limits_snapshot={'model_calls': 999}),
                        replace(run, execution_profile='another-profile')):
            with self.assertRaises(ContractError):
                self.store.save(invalid, self.identity)
        self.assertEqual(self.store.load(run.run_id, self.identity), run)

    async def test_core_checkpoint_recovers_from_postgres_without_repeating_jira(self):
        def interrupt(point, run):
            if point == 'after-result':
                raise SystemExit(2)
        run = self.store.create(self.run_record(), self.identity)
        with self.assertRaises(SystemExit):
            await Harness(ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': FakeJira()},
                          self.store, checkpoint_hook=interrupt).execute(run.run_id, self.identity)
        before = self.store.load(run.run_id, self.identity)
        second = PostgresRunStore(local_dsn(), schema=self.schema)
        jira = FakeJira()
        result = await Harness(ScriptedLLM(), ReadOnlyPolicy(), {'jira_get_issue': jira}, second).resume(run.run_id, self.identity)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(result.results, before.results)
        self.assertEqual(jira.invocations, 0)
        self.assertGreaterEqual(result.active_seconds, before.active_seconds)

    async def test_pending_work_is_not_executed_without_a_claim(self):
        run = self.store.enqueue(self.run_record(), self.identity)
        run.status = RunState.RUNNING
        with self.assertRaises(ContractError) as error:
            self.store.save(run, self.identity)
        self.assertEqual(error.exception.code, 'WORK_NOT_CLAIMED')
        self.assertEqual(self.store.load(run.run_id, self.identity).status, 'QUEUED')
        self.assertEqual(self.store.pending_jobs(self.identity)[0]['status'], 'PENDING')

    async def test_database_foreign_key_blocks_wrong_job_owner(self):
        run = self.store.create(self.run_record(), self.identity)
        with self.assertRaises(ContractError) as error:
            with self.store._database() as db:
                db.execute('INSERT INTO jobs(job_id, run_id, workspace_id, requested_by) VALUES (%s, %s, %s, %s)',
                           (str(uuid4()), run.run_id, 'workspace-beta', self.identity.actor_id))
        self.assertEqual(error.exception.code, 'DATABASE_ERROR')
        self.assertEqual(self.store.pending_jobs(self.identity), [])

    async def test_unknown_schema_version_is_rejected_without_overwrite(self):
        with self.store._database() as db:
            db.execute('UPDATE schema_version SET version = 99')
        with self.assertRaises(ContractError) as error:
            PostgresRunStore(local_dsn(), schema=self.schema)
        self.assertEqual(error.exception.code, 'STORAGE_SCHEMA_UNSUPPORTED')
        with self.store._database() as db:
            self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()['version'], 99)

    async def test_corrupt_payload_or_flat_version_is_rejected(self):
        run = self.store.create(self.run_record(), self.identity)
        with self.store._database() as db:
            db.execute('UPDATE runs SET version = 999 WHERE run_id = %s', (run.run_id,))
        with self.assertRaises(ContractError) as error:
            self.store.load(run.run_id, self.identity)
        self.assertEqual(error.exception.code, 'CORRUPT_STATE')
        with self.store._database() as db:
            db.execute("UPDATE runs SET payload = '{}'::jsonb WHERE run_id = %s", (run.run_id,))
        with self.assertRaises(ContractError) as error:
            self.store.load(run.run_id, self.identity)
        self.assertEqual(error.exception.code, 'CORRUPT_STATE')

    async def test_application_role_has_no_superuser_or_role_creation_privileges(self):
        with self.store._database() as db:
            row = db.execute('SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user').fetchone()
        self.assertEqual(list(row.values()), [False, False, False])

    async def test_mcp_partial_report_roundtrips_jsonb_with_failure_and_assessment(self):
        profile = 'mcp-devops-ci-v2:unavailable'
        run = self.store.create(self.run_record(profile), self.identity)
        result = await compose(profile, self.identity, self.store).harness.execute(run.run_id, self.identity)
        self.assertEqual(result.assessment['status'], 'INCOMPLETE')
        self.assertEqual(len(result.tool_failures), 1)
        restored = PostgresRunStore(local_dsn(), schema=self.schema).load(run.run_id, self.identity)
        self.assertEqual(restored, result)
        self.assertEqual(self.store.pending_jobs(self.identity), [])
