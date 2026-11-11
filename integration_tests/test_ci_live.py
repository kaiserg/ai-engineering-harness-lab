"""Prueba opt-in de API, PostgreSQL, MCP, GitHub público y Jenkins local."""
import asyncio
from dataclasses import replace
import os
import socket
import unittest
from unittest.mock import patch
from uuid import uuid4

from harness.ci_live import load
from harness.composition import compose
from harness.worker import Worker
from integration_tests import test_knowledge_api as api_tests


@unittest.skipUnless(os.environ.get('HARNESS_TEST_LIVE_CI') == '1', 'requiere Jenkins del ejercicio M7f')
class LiveCIIntegrationTests(unittest.TestCase):
    cleanup = api_tests.KnowledgeAPITests.cleanup
    setUp = api_tests.KnowledgeAPITests.setUp
    headers = api_tests.KnowledgeAPITests.headers

    def prepare(self, binding):
        with self.runs._database() as db:
            db.execute('UPDATE user_memberships SET repositories=%s, jobs=%s WHERE actor_id=%s AND workspace_id=%s',
                       ([binding.repository], [binding.job], binding.actor, binding.workspace))
        created = self.client.post('/api/chat/conversations', headers=self.headers(), json={
            'request_key': str(uuid4()), 'mode': 'ci-live'})
        self.assertEqual(created.status_code, 201, created.text)
        return '/api/chat/conversations/' + created.json()['conversation_id']

    def send(self, base, binding, number, pause=False):
        body = {'request_key': str(uuid4()), 'query': f'{binding.repository}#{binding.pull_number} build {number}',
                'pause_after_search': pause}
        sent = self.client.post(base+'/messages', headers=self.headers(), json=body)
        self.assertEqual(sent.status_code, 202, sent.text)
        return sent.json()['run_id']

    def work(self):
        identity = self.knowledge.identity()
        worker = Worker(self.runs, identity=identity, identity_resolver=self.knowledge.identity,
                        composer=lambda profile, actor, store, **options:
                        compose(profile, actor, store, knowledge=self.knowledge, **options))
        return asyncio.run(worker.run_once())

    def turns(self, base):
        response = self.client.get(base, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['turns']

    def test_matching_and_stale_build_with_resume(self):
        binding, _ = load()
        base = self.prepare(binding)
        self.send(base, binding, 1)
        self.assertEqual(self.work()['status'], 'COMPLETED')
        verified = self.turns(base)[0]
        self.assertEqual(verified['assessment']['status'], 'VERIFIED')
        self.assertEqual(verified['assessment']['pr_revision'], verified['assessment']['build_revision'])

        run_id = self.send(base, binding, 2, pause=True)
        self.assertEqual(self.work()['event'], 'paused')
        saved = self.turns(base)[1]['results']
        self.assertEqual(len(saved), 1)
        resumed = self.client.post(f'{base}/runs/{run_id}/resume', headers=self.headers(), json={})
        self.assertEqual(resumed.status_code, 202, resumed.text)
        with patch('harness.adapters.mcp_github_public.MCPGitHubPublic.invoke', side_effect=AssertionError('repeated PR')):
            self.assertEqual(self.work()['status'], 'COMPLETED')
        after = self.turns(base)[1]
        self.assertEqual(after['run_id'], run_id)
        self.assertEqual(after['results'][0], saved[0])
        self.assertEqual(after['assessment']['status'], 'STALE_BUILD')
        self.assertEqual((after['model_calls'], after['tool_calls']), (3, 2))
        self.assertEqual(self.client.get('/api/runs/'+run_id, headers=self.headers()).status_code, 404)

    def test_unavailable_jenkins_abstains(self):
        binding, token = load()
        with socket.socket() as offline:
            offline.bind(('127.0.0.1', 0))
            unreachable = replace(binding, url=f'http://127.0.0.1:{offline.getsockname()[1]}')
            with patch('harness.ci_live.load', return_value=(unreachable, token)):
                base = self.prepare(unreachable)
                self.send(base, unreachable, 1)
                self.assertEqual(self.work()['status'], 'COMPLETED')
                report = self.turns(base)[0]['assessment']
                self.assertEqual(report['status'], 'INCOMPLETE')
                self.assertIsNone(report['build_successful'])
                self.assertEqual(len(report['sources']), 1)
