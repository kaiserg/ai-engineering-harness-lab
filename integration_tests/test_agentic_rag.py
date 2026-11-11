"""Persistencia, citas y recuperación de RAG agéntico con decisiones controladas."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from harness.agentic_rag import AgenticReader, LIMITS
from harness.profiles import AGENTIC_CHAT_PROFILE
from harness.chat import Chats
from harness.checkpoints import RunInterrupted
from harness.composition import compose
from harness.contracts import ContractError
from harness.knowledge import Knowledge
from harness.worker import Worker
from integration_tests import test_knowledge as inventory
from integration_tests.test_rag_agent import AsyncVectors


def first_search(query):
    return json.dumps({'name':'knowledge_search','arguments':{'query':query,'limit':3}})


def search(query):
    return json.dumps({'action':'search','query':query,'answer':'','evidence':[]})


class AgenticRAGTests(unittest.IsolatedAsyncioTestCase):
    cleanup = inventory.KnowledgeIntegrationTests.cleanup

    async def asyncSetUp(self):
        inventory.KnowledgeIntegrationTests.setUp(self)
        self.knowledge = Knowledge(self.runs,self.knowledge.authenticate,AsyncVectors())
        self.doc = self.knowledge.register('atlas.md','Marta coordina Atlas.')['document_id']
        self.knowledge.rebuild()
        self.identity = self.knowledge.identity()
        self.chats = Chats(self.knowledge)
        self.conversation = str(self.chats.create(uuid4(),'documents-agentic')['conversation_id'])

    def worker(self):
        return Worker(self.runs,identity=self.identity,identity_resolver=self.knowledge.identity,
            composer=lambda profile,actor,store,**options:compose(profile,actor,store,knowledge=self.knowledge,**options))

    async def test_second_search_recovers_without_repeating_first_and_can_cite_it(self):
        identifier = self.chats.send(self.conversation,uuid4(),'Atlas',True)
        async def infer(reader,messages,schema,**options):
            results = json.JSONDecoder().raw_decode(messages[-1]['content'])[0]['results'] if 'searches_remaining' in messages[-1]['content'] else []
            if not results:
                return first_search('Atlas')
            if len(results)==1:
                return search('coordinación de entrega Atlas')
            first = results[0]['matches'][0]
            return json.dumps({'action':'answered','query':'','answer':'Marta coordina Atlas.',
                               'evidence':[{'citation':first['citation'],'quote':first['text']}]})
        with patch.object(AgenticReader,'infer',infer):
            self.assertEqual((await self.worker().run_once())['event'],'paused')
            before = self.runs.load(identifier,self.identity)
            self.runs.requeue_job(identifier,self.identity)
            await self.worker().run_once()
        saved = self.runs.load(identifier,self.identity)
        self.assertEqual(saved.status,'COMPLETED',saved.outcome_code)
        self.assertEqual(saved.results[:1],before.results)
        self.assertEqual((saved.model_calls,saved.tool_calls),(3,2))
        self.assertEqual(saved.assessment['knowledge_citations'],[before.results[0].data['matches'][0]['citation']])

    async def test_interrupted_second_search_uses_attempt_reserve(self):
        identifier = self.chats.send(self.conversation,uuid4(),'Atlas')
        def interrupt(point, run):
            if point=='before-tool' and len(run.results)==1:
                raise RunInterrupted()
        abstain = json.dumps({'action':'abstained','query':'','answer':'','evidence':[]})
        with patch.object(AgenticReader,'infer',side_effect=[first_search('Atlas'),search('entrega'),abstain]):
            claim = self.runs.claim_next('interrupted-worker',self.identity)
            execution = compose(AGENTIC_CHAT_PROFILE,self.identity,self.runs.bind(claim),
                                knowledge=self.knowledge,limits=LIMITS,checkpoint_hook=interrupt)
            with self.assertRaises(RunInterrupted):
                await execution.harness.resume(identifier,self.identity)
            before = self.runs.load(identifier,self.identity)
            with self.runs._database() as db:
                db.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",(identifier,))
            self.runs.requeue_job(identifier,self.identity)
            await self.worker().run_once()
        saved = self.runs.load(identifier,self.identity)
        self.assertEqual(saved.status,'COMPLETED',saved.outcome_code)
        self.assertEqual((saved.model_calls,saved.tool_calls,len(saved.results)),(3,3,2))
        self.assertEqual(saved.results[:1],before.results)
        self.assertEqual(saved.results[1].call_id,before.pending_call.call_id)

    async def test_third_search_and_forged_quote_cannot_publish(self):
        for third in (
            search('tercera búsqueda'),
            json.dumps({'action':'answered','query':'','answer':'Inventado',
                        'evidence':[{'citation':'[inventada]','quote':'inventado'}]}),
        ):
            with self.subTest(third=third):
                identifier = self.chats.send(self.conversation,uuid4(),'Atlas')
                with patch.object(AgenticReader,'infer',side_effect=[first_search('Atlas'),search('entrega'),third]):
                    await self.worker().run_once()
                saved = self.runs.load(identifier,self.identity)
                self.assertEqual((saved.status,saved.outcome_code),('FAILED','INVALID_MODEL_OUTPUT'))
                self.assertEqual(saved.tool_calls,2)
                self.assertIsNone(saved.answer)

    async def test_retired_evidence_blocks_recovery_before_inference(self):
        identifier = self.chats.send(self.conversation,uuid4(),'Atlas',True)
        with patch.object(AgenticReader,'infer',return_value=first_search('Atlas')):
            self.assertEqual((await self.worker().run_once())['event'],'paused')
        self.knowledge.retire(self.doc)
        self.assertTrue(self.chats.view(self.conversation)['turns'][0]['unavailable'])
        with self.assertRaisesRegex(ContractError,'EVIDENCE_REVOKED'):
            self.chats.run(self.conversation,identifier)
        self.runs.requeue_job(identifier,self.identity)
        with patch.object(AgenticReader,'infer',side_effect=AssertionError('revoked evidence reached the model')):
            result = await self.worker().run_once()
        self.assertEqual(result.get('error'),'EVIDENCE_REVOKED')
