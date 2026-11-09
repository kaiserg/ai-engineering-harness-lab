"""Reglas reales y receptor local; no inyectar incidentes al ejecutar estas pruebas."""
import json
import subprocess
import unittest
from scripts.incident import get, rules, ROOT, alarm_states
from scripts.stack import compose_command


class AlertComposeTests(unittest.TestCase):
    def test_promtool_checks_rules_and_seven_virtual_time_cases(self):
        subprocess.run(compose_command()+['exec','-T','prometheus','promtool','check','rules','/etc/prometheus/alerts.yml'], check=True, capture_output=True)
        data=(ROOT/'observability/alerts.test.yml').read_text().replace('"alerts.yml"','"/etc/prometheus/alerts.yml"')
        result=subprocess.run(compose_command()+['exec','-T','prometheus','sh','-c',
            'cat > /tmp/alerts.test.yml && promtool test rules /tmp/alerts.test.yml'],
            input=data,text=True,capture_output=True,check=True)
        self.assertIn('SUCCESS',result.stdout)
        self.assertEqual(len(json.loads(data)['tests']),7)

    def test_live_engines_and_local_receiver(self):
        self.assertEqual(len(rules(9090)),3)
        self.assertTrue(all(rule['health']=='ok' for rule in rules(9090)))
        grafana=next(rule for rule in rules(3003) if rule['uid']=='harness-queue-pending')
        self.assertEqual(grafana['health'],'ok')
        self.assertEqual(grafana['duration'],30)
        status=get('http://127.0.0.1:9093/api/v2/status')
        config=status['config']['original']
        self.assertIn('local-only',config)
        for integration in ('email_configs','slack_configs','webhook_configs','pagerduty_configs'):
            self.assertNotIn(integration,config)
        self.assertEqual(alarm_states(),{'prometheus':'inactive','grafana':'inactive'})

    def test_dashboard_applied_and_viewer_cannot_write(self):
        dashboard=get('http://127.0.0.1:3003/apis/dashboard.grafana.app/v1/namespaces/default/dashboards/harness-overview')
        self.assertEqual(len(dashboard['spec']['panels']),10)
        self.assertEqual(dashboard['spec']['panels'][-1]['type'],'alertlist')
        import urllib.request,urllib.error
        request=urllib.request.Request('http://127.0.0.1:3003/api/dashboards/db',data=b'{}',
            headers={'Content-Type':'application/json'},method='POST')
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,403)
