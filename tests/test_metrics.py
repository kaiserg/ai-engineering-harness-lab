import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prometheus_client import generate_latest
from harness import metrics as m
from harness.composition import compose,demo_identity
from harness.contracts import Run,ContractError
from harness.storage import SQLiteRunStore


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous=m.METRICS
        m.METRICS=m.Metrics()
    def tearDown(self):
        m.METRICS=self.previous

    async def test_core_counts_real_calls_and_bounded_labels_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SQLiteRunStore(Path(directory)/'runs.sqlite3')
            identity=demo_identity()
            run=Run(identity.workspace_id,identity.actor_id)
            store.create(run,identity)
            result=await compose('fake-jira-v1',identity,store).harness.execute(run.run_id,identity)
        self.assertEqual((result.model_calls,result.tool_calls),(2,1))
        output=generate_latest(m.METRICS.registry).decode()
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_operations_total',
            {'operation':'model','tool':'none','outcome':'success'}),2)
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_operations_total',
            {'operation':'tool','tool':'jira_get_issue','outcome':'success'}),1)
        for forbidden in (run.run_id,'DEV-123','reader-demo','workspace-alpha','run_id','call_id'):
            self.assertNotIn(forbidden,output)

    async def test_timeout_and_unknown_tool_are_sanitized_and_original_error_preserved(self):
        with self.assertRaises(TimeoutError):
            with m.operation('tool','PRIVATE_SENTINEL'):
                raise TimeoutError('PRIVATE_SENTINEL')
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_operations_total',
            {'operation':'tool','tool':'unknown','outcome':'timeout'}),1)
        self.assertNotIn('PRIVATE_SENTINEL',generate_latest(m.METRICS.registry).decode())

    async def test_attempts_distinguish_pause_and_recovery_without_unique_run_counter(self):
        with m.attempt() as finish:
            finish({'event':'paused'})
        with m.attempt(recovered=True) as finish:
            finish({'event':'finished','status':'COMPLETED'})
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_attempts_total',{'outcome':'paused'}),1)
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_attempts_total',{'outcome':'completed'}),1)
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_recovery_attempts_total'),1)

    async def test_fenced_attempt_reports_loss_without_altering_exception(self):
        with self.assertRaisesRegex(ContractError,'LEASE_LOST'):
            with m.attempt(recovered=True):
                raise ContractError('LEASE_LOST')
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_attempts_total',{'outcome':'lease_lost'}),1)

    async def test_broken_metrics_do_not_replace_business_result_or_error(self):
        m.METRICS=None
        with m.operation('model'):
            result=42
        self.assertEqual(result,42)
        with self.assertRaisesRegex(ContractError,'FORBIDDEN'):
            with m.attempt():
                raise ContractError('FORBIDDEN')

    async def test_snapshot_failure_is_not_exported_as_empty_queue(self):
        m.attach_state(lambda:{'runs':{'COMPLETED':3},'jobs':{'PENDING':2},'expired_leases':1})
        output=generate_latest(m.METRICS.registry).decode()
        self.assertIn('harness_jobs_state{status="PENDING"} 2.0',output)
        self.assertIn('harness_state_collection_success 1.0',output)
        def unavailable():
            raise RuntimeError('PRIVATE_SENTINEL')
        m.attach_state(unavailable)
        output=generate_latest(m.METRICS.registry).decode()
        self.assertIn('harness_state_collection_success 0.0',output)
        self.assertNotIn('harness_jobs_state',output)
        self.assertNotIn('PRIVATE_SENTINEL',output)

    async def test_certificate_callback_tracks_current_value_and_rejects_invalid(self):
        value=[1900000000.0]
        m.attach_certificate(lambda:value[0])
        self.assertIn('harness_certificate_not_after_seconds 1.9e+09',generate_latest(m.METRICS.registry).decode())
        value[0]=float('nan')
        output=generate_latest(m.METRICS.registry).decode()
        self.assertNotIn('harness_certificate_not_after_seconds',output)
        self.assertIn('harness_certificate_collection_success 0.0',output)

    async def test_auth_middleware_counts_status_without_body_or_credentials(self):
        body=b'PRIVATE_SENTINEL'
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':401,'headers':[]})
            await send({'type':'http.response.body','body':body})
        result=[]
        async def send(message):
            result.append(message)
        async def receive():
            return {'type':'http.request','body':body}
        middleware=m.MetricsHTTP(app,endpoint='token')
        await middleware({'type':'http','method':'POST','path':'/token',
            'headers':[(b'authorization',body)]},receive,send)
        self.assertEqual(result[-1]['body'],body)
        self.assertEqual(m.METRICS.registry.get_sample_value('harness_auth_rejections_total',
            {'endpoint':'token','status':'401'}),1)
        self.assertNotIn('PRIVATE_SENTINEL',generate_latest(m.METRICS.registry).decode())
