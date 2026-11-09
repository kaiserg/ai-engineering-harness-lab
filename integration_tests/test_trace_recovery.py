from dataclasses import asdict
import unittest
from uuid import uuid4

from psycopg import sql
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from harness import telemetry as t
from harness.composition import demo_identity
from harness.contracts import Run,ContractError
from harness.core import Limits
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.worker import Worker


class TraceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.schema='harness_it_'+uuid4().hex
        self.store=PostgresRunStore(local_dsn(),schema=self.schema)
        self.identity=demo_identity()
        self.previous=t._provider
        self.provider=TracerProvider()
        self.exporter=InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        t.configure('test',provider=self.provider)

    def tearDown(self):
        with self.store._database() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.provider.shutdown()
        t._provider=self.previous

    async def test_recovery_new_attempt_links_producer_and_previous_without_repeating_jira(self):
        with t.span('api.create',root=True):
            producer=t.carrier()
            run=self.store.enqueue(Run(self.identity.workspace_id,self.identity.actor_id,
                limits_snapshot=asdict(Limits())),self.identity,pause_at='jira')
        self.assertEqual((await Worker(self.store).run_once())['event'],'paused')
        before=self.store.load(run.run_id,self.identity)
        with t.span('api.resume',root=True):
            resumed=t.carrier()
            self.store.requeue_job(run.run_id,self.identity)
        self.assertEqual((await Worker(self.store).run_once())['event'],'finished')
        after=self.store.load(run.run_id,self.identity)
        self.assertEqual(after.results[0],before.results[0])
        self.assertEqual((after.model_calls,after.tool_calls),(2,1))
        attempts=[span for span in self.exporter.get_finished_spans() if span.name=='worker.attempt']
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[0].links[0].context.trace_id,int(producer.split('-')[1],16))
        self.assertEqual({link.context.trace_id for link in attempts[1].links},
            {attempts[0].context.trace_id,int(resumed.split('-')[1],16)})
        self.assertEqual(attempts[1].attributes['run.confirmed_results'],1)
        self.assertNotEqual(attempts[0].context.trace_id,attempts[1].context.trace_id)

    async def test_old_fence_cannot_replace_attempt_trace_context(self):
        run=self.store.enqueue(Run(self.identity.workspace_id,self.identity.actor_id),self.identity)
        old=self.store.claim_next('old',self.identity)
        with self.store._database() as db:
            db.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",(run.run_id,))
        new=self.store.claim_next('new',self.identity)
        with t.span('new',root=True):
            self.store.record_attempt_trace(new,self.identity)
            expected=t.carrier()
        with t.span('old',root=True),self.assertRaisesRegex(ContractError,'LEASE_LOST'):
            self.store.record_attempt_trace(old,self.identity)
        with self.store._database() as db:
            value=db.execute('SELECT previous_traceparent FROM jobs WHERE run_id=%s',(run.run_id,)).fetchone()['previous_traceparent']
        self.assertEqual(value,expected)

    async def test_schema_two_migrates_without_changing_existing_run(self):
        run=self.store.enqueue(Run(self.identity.workspace_id,self.identity.actor_id),self.identity)
        with self.store._database() as db:
            db.execute('ALTER TABLE jobs DROP COLUMN producer_traceparent, DROP COLUMN previous_traceparent, DROP COLUMN telemetry_enqueued_at')
            db.execute('UPDATE schema_version SET version=2')
        migrated=PostgresRunStore(local_dsn(),schema=self.schema)
        self.assertEqual(migrated.load(run.run_id,self.identity),run)
        self.assertIsNotNone(migrated.claim_next('worker',self.identity))

    async def test_trace_context_survives_real_process_death_with_confirmed_evidence(self):
        import subprocess,sys
        run=self.store.enqueue(Run(self.identity.workspace_id,self.identity.actor_id,
            limits_snapshot=asdict(Limits())),self.identity)
        code=("import sys; from opentelemetry.sdk.trace import TracerProvider; "
            "from harness.telemetry import configure; configure('crash-test',provider=TracerProvider()); "
            "from harness.worker import cli; sys.argv=['worker','--once','--schema',sys.argv[1],"
            "'--lease-seconds','3','--crash-at','after-result']; cli()")
        result=subprocess.run([sys.executable,'-c',code,self.schema],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,2)
        before=self.store.load(run.run_id,self.identity)
        with self.store._database() as db:
            previous=db.execute('SELECT previous_traceparent FROM jobs WHERE run_id=%s',(run.run_id,)).fetchone()['previous_traceparent']
            db.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",(run.run_id,))
        self.assertIsNotNone(previous)
        self.assertEqual((await Worker(self.store).run_once())['event'],'finished')
        after=self.store.load(run.run_id,self.identity)
        self.assertEqual(after.results[0],before.results[0])
        self.assertEqual((after.model_calls,after.tool_calls),(2,1))
        attempt=next(span for span in self.exporter.get_finished_spans() if span.name=='worker.attempt')
        self.assertIn(int(previous.split('-')[1],16),{link.context.trace_id for link in attempt.links})
