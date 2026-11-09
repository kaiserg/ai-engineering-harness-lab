"""Jaeger real, correlación durable, privacidad y collector opcional."""
import json
import time
import unittest
import urllib.request
import urllib.error

from test_stack import api,wait_for,docker
from harness.postgres_store import PostgresRunStore
from harness.postgres_config import local_dsn,RUNTIME

BASE='http://127.0.0.1:16686'


def metadata(run_id):
    store=PostgresRunStore(local_dsn())
    with store._database() as db:
        return db.execute('SELECT producer_traceparent,previous_traceparent FROM jobs WHERE run_id=%s',(run_id,)).fetchone()


def trace_id(parent):
    return parent.split('-')[1]


def trace_data(identifier,predicate=lambda spans:True):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen(BASE+'/api/v3/traces/'+identifier,timeout=3) as response:
                chunks=[json.loads(line) for line in response.read().decode().splitlines() if line.strip()]
            resources=[resource for chunk in chunks for resource in chunk.get('result',{}).get('resourceSpans',[])]
            spans=[]
            for resource in resources:
                service=next(item['value']['stringValue'] for item in resource['resource']['attributes'] if item['key']=='service.name')
                for scope in resource['scopeSpans']:
                    for span in scope['spans']:
                        spans.append(dict(span,service=service))
            if spans and predicate(spans):
                return spans
        except (urllib.error.URLError,ValueError,KeyError):
            pass
        time.sleep(.2)
    raise AssertionError('Jaeger did not receive the complete expected trace')


def attributes(span):
    return {item['key']:next(iter(item['value'].values())) for item in span.get('attributes',[])}


class TraceComposeTests(unittest.TestCase):
    def test_distributed_trace_and_recovery_links_preserve_confirmed_jira(self):
        run_id=api('/runs',{'scenario':'matching','pause_at':'jira'})['run_id']
        paused=wait_for(run_id,lambda run:run['job']['status']=='PAUSED')
        first_meta=metadata(run_id)
        first=trace_data(trace_id(first_meta['previous_traceparent']),
            lambda spans:any(span['name']=='worker.attempt' for span in spans) and
                {span['service'] for span in spans}=={'harness-worker','harness-jira','harness-identity'})
        self.assertEqual({span['service'] for span in first},
            {'harness-worker','harness-jira','harness-identity'})
        self.assertEqual(sum(span['name']=='tool.invoke' for span in first),1)
        self.assertEqual(sum(span['name']=='model.decide' for span in first),1)
        worker=next(span for span in first if span['name']=='worker.attempt')
        self.assertIn(trace_id(first_meta['producer_traceparent']),{link['traceId'] for link in worker['links']})
        producer=trace_data(trace_id(first_meta['producer_traceparent']))
        self.assertTrue(any(span['service']=='harness-api' and span['name']=='api.runs.create' for span in producer))
        api('/runs/'+run_id+'/resume',{})
        done=wait_for(run_id,lambda run:run['job']['status']=='DONE')
        self.assertEqual(done['results'][0],paused['results'][0])
        self.assertEqual((done['model_calls'],done['tool_calls']),(4,3))
        second_meta=metadata(run_id)
        self.assertNotEqual(first_meta['previous_traceparent'],second_meta['previous_traceparent'])
        second=trace_data(trace_id(second_meta['previous_traceparent']),
            lambda spans:any(span['name']=='worker.attempt' for span in spans))
        worker=next(span for span in second if span['name']=='worker.attempt')
        self.assertEqual({link['traceId'] for link in worker['links']},
            {trace_id(first_meta['previous_traceparent']),trace_id(second_meta['producer_traceparent'])})
        self.assertEqual(sum(span['name']=='tool.invoke' for span in second),2)
        self.assertFalse(any(span['service']=='harness-jira' for span in second))
        self.assertEqual(int(attributes(worker)['run.confirmed_results']),1)
        self.assertNotIn('producer_traceparent',done['job'])
        serialized=json.dumps(first+second+producer)
        for forbidden in ('Authorization','Bearer ','reader-demo','workspace-alpha','BEGIN PRIVATE KEY','revision-A','Revisar el PR'):
            self.assertNotIn(forbidden,serialized)
        for path in RUNTIME.rglob('*'):
            if path.is_file() and (path.suffix=='.pem' or path.name in ('postgres-app-password','mcp-client-secret')):
                self.assertNotIn(path.read_text().strip(),serialized)

    def test_policy_denial_is_visible_without_sending_forbidden_tool(self):
        run_id=api('/runs',{'scenario':'injection-job'})['run_id']
        run=wait_for(run_id,lambda run:run['job']['status']=='FAILED')
        self.assertEqual(run['outcome_code'],'FORBIDDEN')
        data=metadata(run_id)
        spans=trace_data(trace_id(data['previous_traceparent']),
            lambda spans:any(span['name']=='worker.attempt' for span in spans))
        self.assertTrue(any(span['name']=='policy.authorize' and attributes(span).get('error.code')=='FORBIDDEN' for span in spans))
        self.assertEqual(sum(span['name']=='tool.invoke' for span in spans),1)
        worker=next(span for span in spans if span['name']=='worker.attempt')
        self.assertEqual(worker.get('status',{}).get('code'),2)

    def test_z_collector_outage_does_not_block_run_or_recovery(self):
        try:
            docker('stop','jaeger')
            run_id=api('/runs',{'scenario':'matching','pause_at':'jira'})['run_id']
            before=wait_for(run_id,lambda run:run['job']['status']=='PAUSED')
            api('/runs/'+run_id+'/resume',{})
            after=wait_for(run_id,lambda run:run['job']['status']=='DONE')
            self.assertEqual(after['results'][0],before['results'][0])
            self.assertEqual((after['model_calls'],after['tool_calls']),(4,3))
        finally:
            docker('start','jaeger')
        # Memoria de Jaeger reinicia; dejar una traza nueva y completa verificable.
        run_id=api('/runs',{'scenario':'matching'})['run_id']
        wait_for(run_id,lambda run:run['job']['status']=='DONE')
        data=metadata(run_id)
        spans=trace_data(trace_id(data['previous_traceparent']),
            lambda spans:any(span['name']=='worker.attempt' for span in spans))
        self.assertEqual(sum(span['name']=='model.decide' for span in spans),4)
        self.assertEqual(sum(span['name']=='tool.invoke' for span in spans),3)
        with urllib.request.urlopen(BASE,timeout=3) as response:
            self.assertEqual(response.status,200)
