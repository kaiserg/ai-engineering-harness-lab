"""Prometheus/Grafana reales: queries, permisos Viewer y monitor opcional."""
from datetime import datetime
import json
import http.client
from pathlib import Path
import time
import unittest
import urllib.request
import urllib.parse
import urllib.error

from test_stack import api,wait_for,docker
from harness.service_tls import trust_context
from harness.security_lifecycle import status
from prometheus_client.parser import text_string_to_metric_families

PROM='http://127.0.0.1:9090'
GRAFANA='http://127.0.0.1:3003'
ROOT=Path(__file__).resolve().parents[1]


def get(url):
    with urllib.request.urlopen(url,timeout=8) as response:
        return json.load(response)


def query(expression):
    result=get(PROM+'/api/v1/query?'+urllib.parse.urlencode({'query':expression}))
    if result['status']!='success': raise AssertionError('PromQL query failed')
    return result['data']['result']


def eventually(check,timeout=35):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        try:
            if check(): return
        except (urllib.error.URLError,http.client.HTTPException,TimeoutError,ValueError,KeyError,IndexError): pass
        time.sleep(.4)
    raise AssertionError('Métricas/dashboard no alcanzaron el estado esperado')


def worker_metrics():
    return docker('exec','-T','worker','python','-c',
        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())")


def sample(text,name,labels):
    return sum(point.value for family in text_string_to_metric_families(text) for point in family.samples
        if point.name==name and all(point.labels.get(key)==value for key,value in labels.items()))


class MetricsComposeTests(unittest.TestCase):
    def test_dashboard_is_provisioned_queries_work_and_viewer_cannot_write(self):
        eventually(lambda:len(query('up{job="harness"}'))==4 and all(float(x['value'][1])==1 for x in query('up{job="harness"}')))
        resource=get(GRAFANA+'/apis/dashboard.grafana.app/v1/namespaces/default/dashboards/harness-overview')
        self.assertEqual(resource['metadata']['name'],'harness-overview')
        local=json.loads((ROOT/'observability/grafana/dashboards/harness.json').read_text())
        self.assertEqual(len(local['panels']),10)
        self.assertEqual([t['expr'] for t in resource['spec']['panels'][6]['targets']],
            [t['expr'] for t in local['panels'][6]['targets']])
        for panel in local['panels']:
            for target in panel['targets']:
                query(target['expr'].replace('$__rate_interval','1m'))
        # El rango del panel debe excluir las estimaciones inconsistentes de reinicios.
        now=time.time()
        for target in local['panels'][6]['targets']:
            result=get(PROM+'/api/v1/query_range?'+urllib.parse.urlencode({
                'query':target['expr'].replace('$__rate_interval','1m'),
                'start':now-900,'end':now,'step':10}))
            self.assertEqual(result['status'],'success')
            self.assertFalse(any('monotonicity' in message for message in result.get('infos',[])))
        sources=get(GRAFANA+'/api/datasources')
        source=next(item for item in sources if item['uid']=='harness-prometheus')
        self.assertTrue(source['readOnly'])
        self.assertEqual(source['url'],'http://prometheus:9090')
        # Ejercitar el proxy que usa el navegador, como Viewer anónimo.
        now=int(time.time()*1000)
        payload={'from':str(now-300000),'to':str(now),'queries':[{
            'refId':'A','datasource':{'type':'prometheus','uid':'harness-prometheus'},
            'expr':'sum(up{job="harness"})','instant':True,'format':'time_series',
            'intervalMs':10000,'maxDataPoints':100}]}
        request=urllib.request.Request(GRAFANA+'/api/ds/query',data=json.dumps(payload).encode(),
            headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=10) as response:
            result=json.load(response)
        self.assertNotIn('error',result['results']['A'])
        self.assertTrue(result['results']['A']['frames'])
        request=urllib.request.Request(GRAFANA+'/apis/dashboard.grafana.app/v1/namespaces/default/dashboards/harness-overview',
            method='PUT',data=json.dumps(resource).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request,timeout=5)
        self.assertIn(denied.exception.code,(401,403))

    def test_recovery_metrics_auth_certificates_and_bounded_labels(self):
        before=worker_metrics()
        jira_before=sample(before,'harness_operations_total',{'operation':'tool','tool':'jira_get_issue'})
        recover_before=sample(before,'harness_recovery_attempts_total',{})
        run_id=api('/runs',{'scenario':'matching','pause_at':'jira'})['run_id']
        paused=wait_for(run_id,lambda run:run['job']['status']=='PAUSED')
        eventually(lambda:any(float(x['value'][1])>=1 for x in query('harness_jobs_state{status="PAUSED"}')))
        api('/runs/'+run_id+'/resume',{})
        done=wait_for(run_id,lambda run:run['job']['status']=='DONE')
        self.assertEqual(done['results'][0],paused['results'][0])
        self.assertEqual((done['model_calls'],done['tool_calls']),(4,3))
        after=worker_metrics()
        self.assertEqual(sample(after,'harness_operations_total',{'operation':'tool','tool':'jira_get_issue'})-jira_before,1)
        self.assertEqual(sample(after,'harness_recovery_attempts_total',{})-recover_before,1)
        self.assertNotIn(run_id,after)
        self.assertNotIn('Bearer ',after)
        # Falta de token se observa, conservando el rechazo anterior.
        for port,path in ((8010,'/mcp/'),(8011,'/token')):
            request=urllib.request.Request('https://127.0.0.1:'+str(port)+path,data=b'{}',
                headers={'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request,context=trust_context(),timeout=5)
            self.assertEqual(denied.exception.code,401)
        eventually(lambda:sum(float(x['value'][1]) for x in query('harness_auth_rejections_total{status="401"}'))>=2)
        certificates=status()['certificates']
        for role,name in (('identity','identity'),('jira','jira')):
            expected=datetime.fromisoformat(certificates[name]['expires_at']).timestamp()
            eventually(lambda role=role,expected=expected:any(abs(float(x['value'][1])-expected)<1 for x in query(
                'harness_certificate_not_after_seconds{role="'+role+'"}')))
        series=get(PROM+'/api/v1/series?'+urllib.parse.urlencode({'match[]':'{__name__=~"harness_.*"}'}))['data']
        allowed={'__name__','instance','job','role','operation','tool','outcome','endpoint','status','le'}
        self.assertTrue(all(set(labels)<=allowed for labels in series))

    def test_z_monitor_outage_does_not_block_run_or_recovery(self):
        try:
            docker('stop','prometheus','grafana')
            run_id=api('/runs',{'scenario':'matching','pause_at':'jira'})['run_id']
            before=wait_for(run_id,lambda run:run['job']['status']=='PAUSED')
            api('/runs/'+run_id+'/resume',{})
            after=wait_for(run_id,lambda run:run['job']['status']=='DONE')
            self.assertEqual(before['results'][0],after['results'][0])
            self.assertEqual((after['model_calls'],after['tool_calls']),(4,3))
        finally:
            docker('start','prometheus','grafana')
        eventually(lambda:get(GRAFANA+'/api/health').get('database')=='ok')
        eventually(lambda:len(query('up{job="harness"}'))==4 and all(float(x['value'][1])==1 for x in query('up{job="harness"}')))
