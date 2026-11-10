"""Frontera HTTP real ASGI + PostgreSQL; API no ejecuta la ingestión."""
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from harness.contracts import ContractError
from harness.knowledge_queue import KnowledgeQueue
from harness.knowledge_worker import KnowledgeWorker
from harness.postgres_config import local_dsn
from harness.web import create_app
from integration_tests import test_knowledge as inventory_tests
from integration_tests.test_knowledge_worker import CheckedVectors


class KnowledgeAPITests(unittest.TestCase):
    cleanup=inventory_tests.KnowledgeIntegrationTests.cleanup
    def setUp(self):
        inventory_tests.KnowledgeIntegrationTests.setUp(self)
        app=create_app(storage='postgres',pg_dsn=local_dsn(),pg_schema=self.schema,user_auth=True,user_directory=self.directory)
        app.state.knowledge_vectors=self.vectors
        self.client=TestClient(app,base_url='http://127.0.0.1');self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.beta=self.directory.issue('reader-beta','workspace-beta')

    def headers(self,credential=None):return {'Authorization':'Bearer '+(credential or self.credential)['token'],'X-Harness-Client':'local-ui'}
    def upload(self,**values):
        return self.client.post('/api/knowledge/documents',headers=self.headers(),json={
            'filename':'atlas.md','text':'La responsable del release Atlas es Marta.','request_key':str(uuid4()),**values})
    def enqueue(self,key=None):
        response=self.client.post('/api/knowledge/jobs',headers=self.headers(),json={'request_key':key or str(uuid4())})
        self.assertEqual(response.status_code,202,response.text);return response.json()
    def worker(self):return KnowledgeWorker(KnowledgeQueue(self.knowledge),vectors_factory=lambda check:CheckedVectors(self.vectors,check))

    def test_public_shell_has_no_data_and_fixture_mode_has_no_routes(self):
        page=self.client.get('/knowledge');self.assertEqual(page.status_code,200)
        self.assertIn('console.js',page.text)
        self.assertEqual(page.text,self.client.get('/console').text)
        self.assertEqual(page.text,self.client.get('/').text);self.assertNotIn('Marta',page.text)
        self.assertIn("script-src 'self'",page.headers['content-security-policy'])
        self.assertEqual(self.client.get('/api/knowledge/documents').status_code,401)
        self.assertEqual(self.client.get('/api/knowledge/jobs').status_code,401)
        fixture=create_app()
        self.assertFalse(any(route.path.startswith('/api/knowledge') for route in fixture.routes))

    def test_upload_enqueue_poll_worker_search_and_retire(self):
        uploaded=self.upload();self.assertEqual(uploaded.status_code,201,uploaded.text)
        identifier=uploaded.json()['document_id']
        docs=self.client.get('/api/knowledge/documents',headers=self.headers()).json()['documents']
        self.assertFalse(docs[0]['indexed']);self.assertTrue(docs[0]['can_retire'])
        self.assertNotIn('text',docs[0])
        job=self.enqueue();self.assertEqual(self.vectors.collections,{})
        state=self.client.get('/api/knowledge/jobs',headers=self.headers()).json()['jobs'][0]
        self.assertEqual(state['status'],'PENDING');self.assertNotIn('owner',state);self.assertNotIn('fencing_token',state)
        self.assertEqual(self.worker().run_once()['status'],'DONE')
        self.assertTrue(self.client.get('/api/knowledge/documents',headers=self.headers()).json()['documents'][0]['indexed'])
        search=self.client.post('/api/knowledge/search',headers=self.headers(),json={'query':'responsable Atlas'})
        self.assertIn('Marta',search.json()['matches'][0]['text'])
        self.assertEqual(self.client.post('/api/knowledge/documents/'+identifier+'/retire',headers=self.headers(),json={}).status_code,200)
        self.assertEqual(self.client.post('/api/knowledge/search',headers=self.headers(),json={'query':'Atlas'}).json()['matches'],[])
        self.assertEqual(self.client.get('/api/knowledge/jobs/'+job['job_id'],headers=self.headers()).json()['status'],'DONE')

    def test_upload_retry_is_idempotent_and_cannot_reactivate_retired_document(self):
        key=str(uuid4());first=self.upload(request_key=key).json();second=self.upload(request_key=key).json()
        self.assertEqual(first,second)
        self.assertEqual(self.upload(request_key=key,text='changed payload').status_code,409)
        self.client.post('/api/knowledge/documents/'+first['document_id']+'/retire',headers=self.headers(),json={})
        again=self.upload(request_key=key).json()
        self.assertEqual(again['document_id'],first['document_id']);self.assertEqual(again['state'],'RETIRED')
        with self.runs._database() as db:
            self.assertEqual(db.execute('SELECT count(*) n FROM knowledge_documents').fetchone()['n'],1)
            self.assertEqual(db.execute('SELECT revision FROM knowledge_inventory').fetchone()['revision'],2)

    def test_cross_workspace_documents_jobs_and_retirement_hidden(self):
        identifier=self.upload().json()['document_id'];job=self.enqueue()
        headers=self.headers(self.beta)
        self.assertEqual(self.client.get('/api/knowledge/documents',headers=headers).json()['documents'],[])
        self.assertEqual(self.client.get('/api/knowledge/jobs',headers=headers).json()['jobs'],[])
        self.assertEqual(self.client.get('/api/knowledge/jobs/'+job['job_id'],headers=headers).status_code,404)
        self.assertEqual(self.client.post('/api/knowledge/documents/'+identifier+'/retire',headers=headers,json={}).status_code,404)

    def test_revoked_session_and_current_roles_are_enforced(self):
        self.upload()
        with self.runs._database() as db:db.execute("UPDATE user_memberships SET roles=ARRAY['reader'] WHERE actor_id='reader-demo'")
        self.assertEqual(self.upload().status_code,403)
        response=self.client.post('/api/knowledge/jobs',headers=self.headers(),json={'request_key':str(uuid4())})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.client.get('/api/knowledge/documents',headers=self.headers()).status_code,200)
        self.directory.revoke(self.credential['session_id'])
        self.assertEqual(self.client.get('/api/knowledge/documents',headers=self.headers()).status_code,401)

    def test_payload_and_csrf_guards_do_not_echo_content(self):
        for values in ({'filename':'../secret.md'},{'actor_id':'other'},{'readers':['../bad']},{'text':'\x00PRIVATE_SENTINEL'}):
            response=self.upload(**values);self.assertEqual(response.status_code,422,response.text)
            self.assertNotIn('PRIVATE_SENTINEL',response.text)
        response=self.client.post('/api/knowledge/jobs',headers={**self.headers(),'Origin':'https://evil.example'},json={'request_key':str(uuid4())})
        self.assertEqual(response.status_code,403)
        # Chunked stream no confiable: probar el límite sin reservar MiB para un caso sintético.
        with patch('harness.knowledge_api.BODY_LIMIT',128):
            response=self.client.post('/api/knowledge/documents',headers=self.headers(),content=iter([b'x'*100,b'y'*100]))
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.client.get('/api/knowledge/documents',headers=self.headers()).json()['documents'],[])

    def test_polling_observes_recovery_of_same_job(self):
        self.upload();job=self.enqueue()
        def die():raise KeyboardInterrupt()
        self.vectors.before_build=die
        with self.assertRaises(KeyboardInterrupt):self.worker().run_once()
        first=self.client.get('/api/knowledge/jobs',headers=self.headers()).json()['jobs'][0]
        self.assertEqual(first['status'],'RUNNING');self.assertEqual(first['attempts'],1)
        with self.runs._database() as db:db.execute("UPDATE knowledge_jobs SET lease_until=clock_timestamp()-interval '1 second'")
        expired=self.client.get('/api/knowledge/jobs',headers=self.headers()).json()['jobs'][0]
        self.assertFalse(expired['lease_active'])
        self.vectors.before_build=lambda:None;self.worker().run_once()
        recovered=self.client.get('/api/knowledge/jobs',headers=self.headers()).json()['jobs'][0]
        self.assertEqual(recovered['job_id'],job['job_id']);self.assertEqual(recovered['status'],'DONE')
        self.assertEqual(recovered['attempts'],2);self.assertEqual(first['deadline_at'],recovered['deadline_at'])
