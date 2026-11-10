"""Consola común: shell público sin datos y metadatos con ownership actual."""
import unittest
from harness.contracts import Identity,Run
from integration_tests import test_knowledge_api as api_tests
from integration_tests import test_knowledge as inventory_tests


class ConsoleTests(unittest.TestCase):
    setUp=api_tests.KnowledgeAPITests.setUp
    cleanup=inventory_tests.KnowledgeIntegrationTests.cleanup
    headers=api_tests.KnowledgeAPITests.headers

    def test_one_shell_for_root_console_and_knowledge_with_authenticated_data(self):
        root=self.client.get('/');self.assertEqual(root.status_code,200)
        for path in ('/console','/knowledge'):self.assertEqual(self.client.get(path).text,root.text)
        for label in ('Ejecuciones','Conocimiento','Aprobaciones','Observabilidad','Configuración'):self.assertIn(label,root.text)
        self.assertNotIn('PRIVATE_SENTINEL',root.text)
        self.assertEqual(self.client.get('/api/console/runs').status_code,401)
        self.assertEqual(self.client.get('/api/console/config').status_code,401)
        self.assertIn("script-src 'self'",root.headers['content-security-policy'])

    def test_list_combines_profiles_without_delivering_cached_content_or_other_owners(self):
        own=self.directory.resolve('reader-demo','workspace-alpha')
        ci=self.runs.create(Run(own.workspace_id,own.actor_id,answer='PRIVATE_SENTINEL'),own)
        rag=self.runs.create(Run(own.workspace_id,own.actor_id,execution_profile='mcp-devops-rag-v1'),own)
        for actor,workspace in [('another-reader','workspace-alpha'),('reader-demo','workspace-beta')]:
            other=Identity(actor,workspace,frozenset({'reader'}),frozenset())
            self.runs.create(Run(workspace,actor),other)
        response=self.client.get('/api/console/runs',headers=self.headers());self.assertEqual(response.status_code,200)
        data=response.json()['runs'];self.assertEqual({r['run_id'] for r in data},{ci.run_id,rag.run_id})
        self.assertEqual({r['kind'] for r in data},{'ci','rag'})
        self.assertNotIn('PRIVATE_SENTINEL',response.text)
        self.assertNotIn('answer',response.text);self.assertNotIn('results',response.text)
        self.assertEqual(self.client.get('/api/console/runs',headers=self.headers(self.beta)).json()['runs'],[])

    def test_role_sensitive_console_and_read_only_nonsecret_config(self):
        approver=self.directory.issue('approver-demo','workspace-alpha')
        response=self.client.get('/api/console/config',headers=self.headers(approver));self.assertEqual(response.status_code,200)
        data=response.json();self.assertFalse(data['editable']);self.assertEqual(data['limits']['rag']['model_calls'],5)
        self.assertEqual(data['limits']['ci']['model_calls'],4)
        self.assertEqual(self.client.get('/api/console/runs',headers=self.headers(approver)).status_code,403)
        self.directory.revoke(self.credential['session_id'])
        self.assertEqual(self.client.get('/api/console/config',headers=self.headers()).status_code,401)

    def test_trace_reference_only_comes_from_owned_job_and_valid_carrier(self):
        own=self.directory.resolve('reader-demo','workspace-alpha')
        run=self.runs.enqueue(Run(own.workspace_id,own.actor_id),own)
        valid='00-'+'a'*32+'-'+'b'*16+'-01'
        with self.runs._database() as db:db.execute('UPDATE jobs SET previous_traceparent=%s WHERE run_id=%s',(valid,run.run_id))
        response=self.client.get('/api/console/runs',headers=self.headers()).json()['runs'][0]
        self.assertEqual(response['trace_id'],'a'*32);self.assertNotIn('previous_traceparent',response)
        with self.runs._database() as db:db.execute('UPDATE jobs SET previous_traceparent=%s WHERE run_id=%s',('Bearer PRIVATE_SENTINEL',run.run_id))
        response=self.client.get('/api/console/runs',headers=self.headers())
        self.assertIsNone(response.json()['runs'][0]['trace_id']);self.assertNotIn('PRIVATE_SENTINEL',response.text)
