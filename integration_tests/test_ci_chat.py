"""Admisión, pausa y recuperación de CI desde la API autenticada."""
import asyncio
import unittest
from unittest.mock import patch
from uuid import uuid4

from harness.composition import compose
from harness.worker import Worker
from integration_tests import test_knowledge_api as api_tests


class CIChatAPITests(unittest.TestCase):
    cleanup = api_tests.KnowledgeAPITests.cleanup
    setUp = api_tests.KnowledgeAPITests.setUp
    headers = api_tests.KnowledgeAPITests.headers

    def test_pause_and_recover_stale_build_in_same_turn(self):
        headers = self.headers()
        self.assertIn('ci-stale', self.client.get('/console').text)
        created = self.client.post('/api/chat/conversations', headers=headers, json={
            'request_key': str(uuid4()), 'mode': 'ci-stale'})
        self.assertEqual(created.status_code, 201, created.text)
        base = '/api/chat/conversations/' + created.json()['conversation_id']
        body = {'request_key': str(uuid4()), 'query': '¿Pasó CI para DEV-123?', 'pause_after_search': True}
        sent = self.client.post(base + '/messages', headers=headers, json=body)
        self.assertEqual(sent.status_code, 202, sent.text)
        run_id = sent.json()['run_id']
        self.assertEqual(self.client.post(base + '/messages', headers=headers, json=body).json(), sent.json())
        self.assertEqual(self.client.get('/api/runs/' + run_id, headers=headers).status_code, 404)
        self.assertEqual(self.client.get(base, headers=self.headers(self.beta)).status_code, 404)

        identity = self.knowledge.identity()
        def run_worker():
            worker = Worker(self.runs, identity=identity, identity_resolver=self.knowledge.identity,
                            composer=lambda profile, actor, store, **options:
                            compose(profile, actor, store, knowledge=self.knowledge, **options))
            return asyncio.run(worker.run_once())

        self.assertEqual(run_worker()['event'], 'paused')
        before = self.client.get(base, headers=headers).json()['turns'][0]
        self.assertEqual([r['tool_name'] for r in before['results']], ['jira_get_issue'])
        resumed = self.client.post(base + '/runs/' + run_id + '/resume', headers=headers, json={})
        self.assertEqual(resumed.status_code, 202, resumed.text)
        with patch('harness.adapters.mcp_jira.MCPJira.invoke', side_effect=AssertionError('repeated Jira read')):
            self.assertEqual(run_worker()['status'], 'COMPLETED')
        after = self.client.get(base, headers=headers).json()['turns'][0]
        self.assertEqual(after['run_id'], run_id)
        self.assertEqual(after['assessment']['status'], 'STALE_BUILD')
        self.assertEqual(after['results'][0], before['results'][0])
        self.assertEqual((after['model_calls'], after['tool_calls']), (4, 3))
        self.assertEqual(self.client.get('/api/console/runs', headers=headers).json()['runs'][0]['kind'], 'chat')
