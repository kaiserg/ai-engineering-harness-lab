from dataclasses import replace
import unittest
from uuid import uuid4
from psycopg import sql
from prometheus_client import generate_latest

from harness import metrics as m
from harness.composition import demo_identity
from harness.contracts import Run
from harness.postgres_store import PostgresRunStore
from harness.postgres_config import local_dsn
from harness.worker import Worker


class MetricsStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.schema='harness_it_'+uuid4().hex
        self.store=PostgresRunStore(local_dsn(),schema=self.schema)
        self.identity=demo_identity()
        self.previous=m.METRICS
        m.METRICS=m.Metrics()
    def tearDown(self):
        with self.store._database() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        m.METRICS=self.previous

    async def test_paused_recovered_job_is_one_completed_run_and_two_attempts(self):
        run=self.store.enqueue(Run(self.identity.workspace_id,self.identity.actor_id),self.identity,pause_at='jira')
        self.assertEqual((await Worker(self.store).run_once())['event'],'paused')
        before=self.store.load(run.run_id,self.identity)
        snapshot=self.store.operational_snapshot(self.identity)
        self.assertEqual(snapshot['jobs'],{'PAUSED':1})
        self.assertEqual(snapshot['runs'],{'RUNNING':1})
        self.store.requeue_job(run.run_id,self.identity)
        self.assertEqual((await Worker(self.store).run_once())['event'],'finished')
        after=self.store.load(run.run_id,self.identity)
        self.assertEqual(after.results[0],before.results[0])
        self.assertEqual((after.model_calls,after.tool_calls),(2,1))
        self.assertEqual(self.store.operational_snapshot(self.identity),
            {'runs':{'COMPLETED':1},'jobs':{'DONE':1},'expired_leases':0})
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_recovery_attempts_total'),1)
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_operations_total',
            {'operation':'tool','tool':'jira_get_issue','outcome':'success'}),1)

    async def test_expired_lease_is_observed_then_recovered_without_observer_mutation(self):
        run=self.store.enqueue(Run(self.identity.workspace_id,self.identity.actor_id),self.identity)
        old=self.store.claim_next('old',self.identity)
        with self.store._database() as db:
            db.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",(run.run_id,))
        before=self.store.job_state(run.run_id,self.identity)
        m.attach_state(lambda:self.store.operational_snapshot(self.identity))
        output=generate_latest(m.METRICS.registry).decode()
        self.assertIn('harness_jobs_expired_lease 1.0',output)
        self.assertEqual(self.store.job_state(run.run_id,self.identity)['fencing_token'],old.fencing_token)
        self.assertEqual((await Worker(self.store).run_once())['event'],'finished')
        self.assertEqual(self.store.operational_snapshot(self.identity)['expired_leases'],0)
        self.assertGreater(self.store.job_state(run.run_id,self.identity)['fencing_token'],old.fencing_token)

    async def test_snapshot_uses_trusted_identity_scope_and_exports_no_identity_labels(self):
        other=replace(self.identity,workspace_id='private-workspace')
        self.store.enqueue(Run(other.workspace_id,other.actor_id),other)
        self.assertEqual(self.store.operational_snapshot(self.identity)['runs'],{})
        m.attach_state(lambda:self.store.operational_snapshot(self.identity))
        output=generate_latest(m.METRICS.registry).decode()
        self.assertNotIn(other.workspace_id,output)
        self.assertNotIn(other.actor_id,output)
        self.assertNotIn('workspace',output)
