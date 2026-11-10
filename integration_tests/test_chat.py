"""Chat: admisión atómica/idempotente, aislamiento, contexto y recuperación."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import unittest
from unittest.mock import patch
from harness.chat import Chats,ChatPolicy,PROFILE
from harness.composition import compose
from harness.contracts import ContractError,Identity
from harness.llm_reader import LIMITS
from harness.checkpoints import RunInterrupted
from harness.knowledge import Knowledge
from harness.worker import Worker
from integration_tests import test_knowledge as inventory_tests
from integration_tests import test_knowledge_api as api_tests
from integration_tests.test_rag_agent import AsyncVectors
from integration_tests.test_llm_reader import decision


class ChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    cleanup=inventory_tests.KnowledgeIntegrationTests.cleanup
    async def asyncSetUp(self):
        inventory_tests.KnowledgeIntegrationTests.setUp(self)
        self.vectors=AsyncVectors();self.knowledge=Knowledge(self.runs,lambda:self.directory.resolve('reader-demo','workspace-alpha'),self.vectors)
        self.identity=self.knowledge.identity();self.chats=Chats(self.knowledge)
        self.doc=self.knowledge.register('atlas.md','Marta coordina Atlas.')['document_id'];self.knowledge.rebuild()
        self.id=str(self.chats.create(uuid4())['conversation_id'])
        p=patch('harness.llm_reader.OllamaReader.decide',decision);p.start();self.addCleanup(p.stop)
    def send(self,query='Atlas',key=None,pause=False):return self.chats.send(self.id,key or uuid4(),query,pause)
    def execution(self,store=None,**kwargs):return compose(PROFILE,self.identity,store or self.runs,knowledge=self.knowledge,limits=kwargs.pop('limits',LIMITS),**kwargs)
    def worker(self,**kwargs):return Worker(self.runs,identity=self.identity,identity_resolver=self.knowledge.identity,
        composer=lambda profile,identity,store,**options:self.execution(store,**options),**kwargs)
    async def complete(self,identifier):
        await self.worker().run_once()
        return self.runs.load(identifier,self.identity)

    async def test_atomic_turn_run_job_and_duplicate_key(self):
        key=uuid4();identifier=self.send(key=key)
        self.assertEqual(self.send(key=key),identifier)
        with self.runs._database() as db:
            for table in ('conversation_turns','runs','jobs'):self.assertEqual(db.execute('SELECT count(*) AS n FROM '+table).fetchone()['n'],1)
        with self.assertRaisesRegex(ContractError,'CONFLICT'):self.send(query='changed',key=key)
        with self.assertRaisesRegex(ContractError,'CONFLICT'):self.send(key=key,pause=True)
        with self.assertRaisesRegex(ContractError,'RUN_BUSY'):self.send()

    async def test_rollback_does_not_leave_an_orphan_run_or_job(self):
        original=self.runs._admit
        def fail(*args,**kwargs):original(*args,**kwargs);raise ContractError('INTERNAL_ERROR')
        with patch.object(self.runs,'_admit',side_effect=fail):
            with self.assertRaises(ContractError):self.send()
        with self.runs._database() as db:
            for table in ('conversation_turns','runs','jobs'):self.assertEqual(db.execute('SELECT count(*) AS n FROM '+table).fetchone()['n'],0)

    async def test_concurrent_sends_share_one_run_with_same_key(self):
        key=uuid4()
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.send(key=key),range(2)))
        self.assertEqual(results[0],results[1])

    async def test_history_selection_and_budgets_are_per_run(self):
        ids=[]
        for n in range(5):
            identifier=self.send('Atlas '+str(n));ids.append(identifier);await self.complete(identifier)
        saved=self.runs.load(ids[-1],self.identity)
        self.assertEqual(saved.read_request['history_run_ids'],ids[-4:-1])
        self.assertEqual((saved.model_calls,saved.tool_calls),(2,1))
        history=ChatPolicy(self.knowledge).history(saved,self.identity)
        self.assertEqual([r.run_id for r in history],ids[-4:-1])
        self.assertEqual(len(self.chats.view(self.id)['turns']),5)

    async def test_pause_recovery_reuses_search_and_frozen_history(self):
        first=self.send();await self.complete(first);second=self.send('¿Y su correo?')
        def pause(point,snapshot):
            if point=='after-result':raise RunInterrupted()
        self.assertEqual((await self.worker(checkpoint_hook=pause).run_once())['event'],'paused')
        before=self.runs.load(second,self.identity)
        self.vectors.aquery=lambda *a:(_ for _ in ()).throw(AssertionError('repeated search'))
        self.runs.requeue_job(second,self.identity)
        result=await self.complete(second)
        self.assertEqual(result.status,'COMPLETED');self.assertEqual(result.read_request,before.read_request)
        self.assertEqual(result.results,before.results);self.assertEqual((result.model_calls,result.tool_calls),(2,1))

    async def test_retired_history_is_hidden_and_cannot_reach_model(self):
        first=self.send();await self.complete(first);second=self.send('¿Y su correo?')
        self.knowledge.retire(self.doc)
        data=self.chats.view(self.id)
        self.assertTrue(all(turn['unavailable'] for turn in data['turns']))
        self.assertNotIn('Marta',str(data))
        with self.assertRaisesRegex(ContractError,'EVIDENCE_REVOKED'):await self.execution().harness.resume(second,self.identity)

    async def test_old_dependency_outside_prompt_window_is_still_reauthorized(self):
        first=self.send();await self.complete(first)
        second_doc=self.knowledge.register('other.md','Marta coordina otro producto.')['document_id'];self.knowledge.rebuild()
        original=self.vectors.query
        self.vectors.query=lambda name,vector,documents,limit:original(name,vector,[second_doc],limit)
        ids=[first]
        for _ in range(4):
            identifier=self.send();ids.append(identifier);await self.complete(identifier)
        current=self.runs.load(ids[-1],self.identity)
        self.assertNotIn(first,current.read_request['history_run_ids'])
        self.knowledge.retire(self.doc)
        with self.assertRaisesRegex(ContractError,'EVIDENCE_REVOKED'):ChatPolicy(self.knowledge).authorize_context(current,self.identity)

    async def test_fresh_store_restores_history_and_retirement_during_model_blocks_publication(self):
        first=self.send();await self.complete(first)
        from harness.postgres_store import PostgresRunStore
        fresh=Chats(Knowledge(PostgresRunStore(self.runs._dsn,schema=self.schema),self.knowledge.identity,self.vectors))
        self.assertEqual(fresh.view(self.id)['turns'][0]['run_id'],first)
        second=self.send('¿Y su correo?')
        async def revoke(model,run,allowed):
            self.knowledge.retire(self.doc)
            return await decision(model,run,allowed)
        with patch('harness.llm_reader.OllamaReader.decide',revoke):result=await self.complete(second)
        self.assertEqual(result.status,'FAILED');self.assertEqual(result.outcome_code,'EVIDENCE_REVOKED')
        self.assertIsNone(result.answer)

    async def test_other_owner_and_workspace_do_not_read_or_append(self):
        for actor,workspace in [('reader-beta','workspace-beta'),('someone','workspace-alpha')]:
            identity=Identity(actor,workspace,frozenset({'reader'}),frozenset())
            other=Chats(Knowledge(self.runs,lambda:identity,self.vectors))
            self.assertEqual(other.recent(),[])
            with self.assertRaisesRegex(ContractError,'NOT_FOUND'):other.view(self.id)
            with self.assertRaisesRegex(ContractError,'NOT_FOUND'):other.send(self.id,uuid4(),'Atlas')


class ChatAPITests(unittest.TestCase):
    cleanup=api_tests.KnowledgeAPITests.cleanup
    setUp=api_tests.KnowledgeAPITests.setUp
    headers=api_tests.KnowledgeAPITests.headers

    def test_create_retry_send_and_reload_without_inference(self):
        key=str(uuid4());path='/api/chat/conversations'
        create=lambda:self.client.post(path,headers=self.headers(),json={'request_key':key})
        first=create();self.assertEqual(first.status_code,201,first.text)
        identifier=first.json()['conversation_id'];self.assertEqual(create().json()['conversation_id'],identifier)
        body={'request_key':str(uuid4()),'query':'Atlas'}
        with patch('harness.llm_reader.OllamaReader.decide',side_effect=AssertionError('inline inference')):
            send=self.client.post(path+'/'+identifier+'/messages',headers=self.headers(),json=body)
        self.assertEqual(send.status_code,202,send.text)
        self.assertEqual(self.client.post(path+'/'+identifier+'/messages',headers=self.headers(),json=body).json(),send.json())
        view=self.client.get(path+'/'+identifier,headers=self.headers()).json()
        self.assertEqual(len(view['turns']),1);self.assertEqual(view['turns'][0]['model_calls'],0)
        self.assertEqual(self.client.get('/api/runs/'+send.json()['run_id'],headers=self.headers()).status_code,404)
        self.assertEqual(self.client.get(path+'/'+identifier,headers=self.headers(self.beta)).status_code,404)
        self.assertEqual(self.client.get(path).status_code,401)

    def test_request_cannot_choose_history_or_model_or_identity(self):
        path='/api/chat/conversations';identifier=self.client.post(path,headers=self.headers(),json={'request_key':str(uuid4())}).json()['conversation_id']
        for extra in ('history_run_ids','model','workspace_id','profile'):
            result=self.client.post(path+'/'+identifier+'/messages',headers=self.headers(),json={'request_key':str(uuid4()),'query':'Atlas',extra:'injected'})
            self.assertEqual(result.status_code,422,result.text)
