"""Trazas persistidas como correlación, nunca como permiso ni resultado."""
from dataclasses import replace
import unittest
from unittest.mock import patch
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import test_operations as fixtures
from harness import telemetry as t
from harness import write_observability as w
from harness.write_queue import WriteQueue
from harness.write_worker import WriteWorker


class WriteObservabilityTests(unittest.IsolatedAsyncioTestCase):
    code=fixtures.OperationTests.code
    decide=fixtures.OperationTests.decide
    approved=fixtures.OperationTests.approved
    effects=fixtures.OperationTests.effects
    asyncTearDown=fixtures.OperationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.OperationTests.asyncSetUp(self)
        self.queue=WriteQueue(self.ops);self.op=self.approved()
        self.previous=t._provider
        self.trace_provider=TracerProvider();self.exporter=InMemorySpanExporter()
        self.trace_provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        t.configure('write-test',provider=self.trace_provider)
        self.addCleanup(self.restore)

    def restore(self):
        self.trace_provider.shutdown();t._provider=self.previous

    async def test_claim_links_producer_and_previous_attempt_without_public_headers(self):
        with t.span('producer',root=True):
            producer=t.carrier();public=self.queue.enqueue(self.op['operation_id'],self.operator)
        self.assertNotIn('producer_traceparent',public)
        first=self.queue.claim('first',self.operator)
        self.assertEqual(first.producer_traceparent,producer)
        self.assertEqual(first.attempts,1)
        with t.span('previous',root=True):previous=t.carrier()
        self.queue.record_trace(first,previous)
        self.queue.finish(first,status='PENDING')
        with patch.object(w,'METRICS',None):
            result=await WriteWorker(self.ops,self.operator).run_once()
        self.assertEqual(result['status'],'DONE');self.assertEqual(self.effects(),1)
        root=next(s for s in self.exporter.get_finished_spans() if s.name=='write.worker.attempt')
        self.assertEqual({s.context.trace_id for s in root.links},{int(producer.split('-')[1],16),int(previous.split('-')[1],16)})
        self.assertEqual(root.attributes['job.attempt'],2)
        self.assertNotIn('previous_traceparent',self.queue.get(self.op['operation_id'],self.operator))
        self.code('LEASE_LOST',lambda:self.queue.record_trace(first,previous))

    async def test_snapshot_is_scoped_readonly_and_state_survives_monitor_failure(self):
        self.queue.enqueue(self.op['operation_id'],self.operator)
        before=self.queue.get(self.op['operation_id'],self.operator)
        snap=self.queue.snapshot(self.operator)
        self.assertEqual(snap['jobs'],{'PENDING':1});self.assertEqual(snap['operations'],{'PREPARED':1})
        self.assertEqual(self.queue.get(self.op['operation_id'],self.operator),before)
        other=replace(self.operator,workspace_id='other-workspace')
        self.code('FORBIDDEN',lambda:self.queue.snapshot(other))
        with patch.object(WriteQueue,'record_trace',side_effect=RuntimeError),patch.object(w,'METRICS',None):
            result=await WriteWorker(self.ops,self.operator).run_once()
        self.assertEqual(result['status'],'DONE');self.assertEqual(self.effects(),1)
        self.assertEqual(self.queue.snapshot(self.operator)['operations'],{'SUCCEEDED':1})
