"""Observación opcional: sin datos de negocio ni dependencia del exporter."""
import unittest
from unittest.mock import patch
from prometheus_client import generate_latest
from harness import write_observability as w


class WriteObservabilityTests(unittest.TestCase):
    def test_snapshot_failure_is_missing_data_not_zero_backlog(self):
        metrics=w.WriteMetrics()
        metrics.snapshot=lambda:(_ for _ in ()).throw(RuntimeError('PRIVATE_SENTINEL'))
        text=generate_latest(metrics.registry).decode()
        self.assertIn('harness_write_collection_success 0.0',text)
        self.assertNotIn('harness_write_jobs_state',text)
        self.assertNotIn('PRIVATE_SENTINEL',text)

    def test_snapshot_validates_states_and_nonnegative_counts(self):
        metrics=w.WriteMetrics()
        data={'jobs':{'PENDING':2},'operations':{'PREPARED':2},'expired_leases':0,'oldest_pending_seconds':3.5}
        metrics.snapshot=lambda:data
        text=generate_latest(metrics.registry).decode()
        self.assertIn('harness_write_jobs_state{status="PENDING"} 2.0',text)
        self.assertIn('harness_write_collection_success 1.0',text)
        for invalid in ({'SECRET':1},{'PENDING':-1},{'PENDING':True}):
            data['jobs']=invalid
            text=generate_latest(metrics.registry).decode()
            self.assertIn('harness_write_collection_success 0.0',text)
            self.assertNotIn('SECRET',text)

    def test_metrics_failure_preserves_result_and_original_error(self):
        with patch.object(w,'METRICS',None):
            with w.client_call('request'):
                result=42
            with w.worker_attempt() as finish:finish({'status':'DONE'})
            self.assertEqual(result,42)
            with self.assertRaisesRegex(ValueError,'original'):
                with w.client_call('lookup'):raise ValueError('original')

    def test_labels_are_bounded_and_never_use_arbitrary_action(self):
        metrics=w.WriteMetrics()
        with patch.object(w,'METRICS',metrics):
            with w.client_call('PRIVATE_SENTINEL'):pass
            with w.worker_attempt() as finish:finish({'status':'PRIVATE_SENTINEL'})
        text=generate_latest(metrics.registry).decode()
        self.assertNotIn('PRIVATE_SENTINEL',text)
        self.assertIn('action="unknown",outcome="success"',text)
        self.assertIn('result="error"',text)

    def test_exporter_start_failure_does_not_raise(self):
        with patch.dict('os.environ',{'HARNESS_WRITE_METRICS':'1'}),patch.object(w,'_server',None),patch.object(w,'start_http_server',side_effect=OSError):
            w.start(9102,lambda:{})
