"""Recorrido opt-in de Jira Cloud, GitHub público y Jenkins local reales."""
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from harness import jira_cloud
from harness.ci_live import load
from integration_tests import test_ci_live as live
from integration_tests import test_knowledge_api as api_tests


@unittest.skipUnless(os.environ.get('HARNESS_TEST_JIRA_CI_ISSUE'), 'requiere ticket Jira vinculado al PR del ejercicio')
class JiraCIIntegrationTests(unittest.TestCase):
    cleanup = api_tests.KnowledgeAPITests.cleanup
    setUp = api_tests.KnowledgeAPITests.setUp
    headers = api_tests.KnowledgeAPITests.headers
    work = live.LiveCIIntegrationTests.work
    turns = live.LiveCIIntegrationTests.turns

    def test_real_ticket_matching_and_stale_build_with_resume(self):
        key = os.environ['HARNESS_TEST_JIRA_CI_ISSUE']
        binding, _ = load()
        with self.runs._database() as db:
            db.execute('UPDATE user_memberships SET repositories=%s, jobs=%s, projects=%s WHERE actor_id=%s AND workspace_id=%s',
                       ([binding.repository], [binding.job], jira_cloud.load(jira_cloud.current())['projects'],
                        binding.actor, binding.workspace))
        headers = self.headers()
        response = self.client.post('/api/chat/conversations', headers=headers, json={
            'request_key':str(uuid4()), 'mode':'ci-jira'})
        self.assertEqual(response.status_code,201,response.text)
        base = '/api/chat/conversations/'+response.json()['conversation_id']

        def send(number, pause=False):
            body = {'request_key':str(uuid4()),'query':f'¿Pasó el build {number} de {key}?','pause_after_search':pause}
            sent = self.client.post(base+'/messages', headers=headers, json=body)
            self.assertEqual(sent.status_code,202,sent.text)
            self.assertEqual(self.client.post(base+'/messages', headers=headers, json=body).json(),sent.json())
            return sent.json()['run_id']

        send(1)
        self.assertEqual(self.work()['status'],'COMPLETED')
        report = self.turns(base)[0]['assessment']
        self.assertEqual(report['status'],'VERIFIED')
        self.assertEqual(len(report['sources']),3)
        run_id = send(2,pause=True)
        self.assertEqual(self.work()['event'],'paused')
        before = self.turns(base)[1]['results']
        self.assertEqual([r['tool_name'] for r in before],['jira_get_issue'])
        resumed = self.client.post(f'{base}/runs/{run_id}/resume',headers=headers,json={})
        self.assertEqual(resumed.status_code,202,resumed.text)
        with patch('harness.adapters.mcp_jira_cloud.MCPJiraCloud.invoke',side_effect=AssertionError('repeated Jira')):
            self.assertEqual(self.work()['status'],'COMPLETED')
        after = self.turns(base)[1]
        self.assertEqual(after['run_id'],run_id)
        self.assertEqual(after['results'][0],before[0])
        self.assertEqual(after['assessment']['status'],'STALE_BUILD')
        self.assertEqual((after['model_calls'],after['tool_calls']),(4,3))
        self.assertEqual(self.client.get(base,headers=self.headers(self.beta)).status_code,404)
        self.assertEqual(self.client.get('/api/runs/'+run_id,headers=headers).status_code,404)
