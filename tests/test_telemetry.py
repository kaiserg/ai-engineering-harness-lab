import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from harness import telemetry as t
from harness.composition import compose,demo_identity
from harness.contracts import ContractError,Run
from harness.storage import SQLiteRunStore


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous=t._provider
        self.provider=TracerProvider()
        self.exporter=InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        t.configure('test',provider=self.provider)

    def tearDown(self):
        self.provider.shutdown()
        t._provider=self.previous

    async def test_w3c_random_id_flag_and_baggage_is_not_propagated(self):
        with t.span('producer',root=True):
            value=t.carrier()
            self.assertIsNotNone(value)
            self.assertEqual(t.carrier(value[:-2]+'03'),value[:-2]+'03')
            self.assertEqual(set(t.headers()),{'traceparent'})
            with t.span('consumer',parent=value):
                self.assertEqual(trace.get_current_span().get_span_context().trace_id,
                    trace.get_current_span(t.context(value)).get_span_context().trace_id)
        for bad in ('x'*1000,'00-'+'0'*32+'-'+'0'*16+'-01', 'Bearer SECRET'):
            self.assertIsNone(t.carrier(bad))

    async def test_async_tasks_keep_context_and_attempt_links_have_new_trace(self):
        with t.span('producer',root=True):
            parent=t.carrier()
            async def task():
                with t.span('child'):
                    await asyncio.sleep(0)
            await asyncio.gather(task(),task())
        with t.span('attempt',root=True,links=[parent]):
            attempt=t.carrier()
        spans=self.exporter.get_finished_spans()
        producer=next(span for span in spans if span.name=='producer')
        children=[span for span in spans if span.name=='child']
        self.assertTrue(all(span.parent.span_id==producer.context.span_id for span in children))
        root=next(span for span in spans if span.name=='attempt')
        self.assertNotEqual(root.context.trace_id,producer.context.trace_id)
        self.assertEqual(root.links[0].context.trace_id,producer.context.trace_id)

    async def test_allowlist_and_exception_metadata_do_not_export_secrets(self):
        with self.assertRaises(RuntimeError):
            with t.span('tool.invoke',values={'Authorization':'Bearer PRIVATE_SENTINEL',
                    'prompt':'PRIVATE_SENTINEL','tool.name':'PRIVATE_SENTINEL','error.code':'PRIVATE_SENTINEL'}):
                raise RuntimeError('PRIVATE_SENTINEL')
        span=self.exporter.get_finished_spans()[0]
        self.assertEqual(span.attributes['tool.name'],'unknown')
        self.assertEqual(span.attributes['error.code'],'INTERNAL_ERROR')
        self.assertEqual(span.status.status_code,trace.StatusCode.ERROR)
        self.assertFalse(span.events)
        self.assertNotIn('PRIVATE_SENTINEL',span.to_json())

    async def test_broken_tracer_does_not_swallow_business_result_or_error(self):
        class Broken:
            def get_tracer(self,*args):
                raise RuntimeError('exporter unavailable')
        t._provider=Broken()
        with t.span('test'):
            result=42
        self.assertEqual(result,42)
        with self.assertRaisesRegex(ContractError,'FORBIDDEN'):
            with t.span('test'):
                raise ContractError('FORBIDDEN')

    async def test_core_has_same_counts_and_separate_model_tool_spans(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SQLiteRunStore(Path(directory)/'runs.sqlite3')
            identity=demo_identity()
            run=Run(identity.workspace_id,identity.actor_id)
            store.create(run,identity)
            with t.span('attempt',root=True):
                result=await compose('fake-jira-v1',identity,store).harness.execute(run.run_id,identity)
        self.assertEqual((result.model_calls,result.tool_calls),(2,1))
        spans=self.exporter.get_finished_spans()
        self.assertEqual(sum(span.name=='model.decide' for span in spans),2)
        self.assertEqual(sum(span.name=='tool.invoke' for span in spans),1)
        self.assertEqual(sum(span.name=='policy.authorize' for span in spans),2)

    async def test_bad_export_destination_leaves_tracing_disabled(self):
        t._provider=None
        with patch.dict('os.environ',{'HARNESS_TRACING':'1','HARNESS_OTLP_ENDPOINT':'https://example.invalid/secret'}):
            self.assertIsNone(t.configure('test'))

    async def test_write_attributes_are_bounded_and_payload_is_excluded(self):
        values=t.attributes({'operation.id':'12345678-1234-1234-1234-123456789abc',
            'proposal.id':'PRIVATE_SENTINEL','write.phase':'lookup','write.state':'UNKNOWN',
            'arguments':'PRIVATE_SENTINEL','payload_hash':'PRIVATE_SENTINEL','actor_id':'PRIVATE_SENTINEL'})
        self.assertEqual(set(values),{'operation.id','write.phase','write.state'})
        self.assertEqual(t.attributes({'write.phase':'PRIVATE_SENTINEL','write.state':'PRIVATE_SENTINEL'}),{})
