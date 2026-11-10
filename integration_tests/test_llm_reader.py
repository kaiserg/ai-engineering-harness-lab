"""Lector real enchufado al mismo core; modelo simulado solo en estas pruebas de controles."""
from dataclasses import asdict
import json
from unittest.mock import patch
import unittest
from harness.contracts import Run,ToolCall,FinalAnswer,ContractError
from harness.checkpoints import RunInterrupted
from harness.composition import compose
from harness.core import Limits
from harness.llm_reader import PROFILE,LIMITS
from harness.worker import Worker
from integration_tests.test_rag_agent import AsyncVectors
from integration_tests import test_knowledge as inventory_tests
from integration_tests import test_knowledge_api as api_tests


async def decision(model,run,allowed):
    if not run.results:return ToolCall('knowledge_search',{'query':run.read_request['query'],'limit':3})
    matches=run.results[0].data['matches']
    data={'status':'abstained','answer':'','evidence':[]}
    if matches:data={'status':'answered','answer':'Marta','evidence':[{'citation':matches[0]['citation'],'quote':matches[0]['text']}]}
    return FinalAnswer(json.dumps(data))


class ReaderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    cleanup=inventory_tests.KnowledgeIntegrationTests.cleanup
    async def asyncSetUp(self):
        inventory_tests.KnowledgeIntegrationTests.setUp(self)
        self.vectors=AsyncVectors()
        from harness.knowledge import Knowledge
        self.knowledge=Knowledge(self.runs,lambda:self.directory.resolve('reader-demo','workspace-alpha'),self.vectors)
        self.identity=self.knowledge.identity()
        self.knowledge.register('atlas.md','Marta coordina Atlas.');self.knowledge.rebuild()
        self.model_patch=patch('harness.llm_reader.OllamaReader.decide',decision);self.model_patch.start();self.addCleanup(self.model_patch.stop)

    def execution(self,store=None,**kwargs):
        return compose(PROFILE,self.identity,store or self.runs,knowledge=self.knowledge,limits=kwargs.pop('limits',LIMITS),**kwargs)
    def create(self,limits=LIMITS):return self.runs.create(Run(self.identity.workspace_id,self.identity.actor_id,
        execution_profile=PROFILE,read_request={'query':'¿Quién coordina Atlas?'},limits_snapshot=asdict(limits)),self.identity)

    async def test_core_completes_with_real_profile_and_checked_citations(self):
        run=self.create();result=await self.execution().harness.execute(run.run_id,self.identity)
        self.assertEqual(result.status,'COMPLETED',result.outcome_code)
        self.assertEqual((result.model_calls,result.tool_calls),(2,1));self.assertEqual(result.assessment['answer_status'],'ANSWERED')
        self.assertIn('Marta',result.answer)

    async def test_recovery_reuses_committed_search_and_preserves_question(self):
        run=self.create()
        def pause(point,snapshot):
            if point=='after-result':raise RunInterrupted()
        with self.assertRaises(RunInterrupted):await self.execution(checkpoint_hook=pause).harness.execute(run.run_id,self.identity)
        before=self.runs.load(run.run_id,self.identity)
        self.vectors.aquery=lambda *args:(_ for _ in ()).throw(AssertionError('repeated retrieval'))
        result=await self.execution().harness.resume(run.run_id,self.identity)
        self.assertEqual(result.status,'COMPLETED');self.assertEqual(result.read_request,before.read_request)
        self.assertEqual(result.results,before.results);self.assertEqual((result.model_calls,result.tool_calls),(2,1))
        self.assertGreaterEqual(result.active_seconds,before.active_seconds)

    async def test_retired_evidence_cannot_reach_model_on_resume(self):
        run=self.create()
        def pause(point,snapshot):
            if point=='after-result':raise RunInterrupted()
        with self.assertRaises(RunInterrupted):await self.execution(checkpoint_hook=pause).harness.execute(run.run_id,self.identity)
        doc=self.knowledge.documents()[0]['document_id'];self.knowledge.retire(doc)
        with self.assertRaisesRegex(ContractError,'EVIDENCE_REVOKED'):await self.execution().harness.resume(run.run_id,self.identity)

    async def test_budget_not_reset_to_allow_final_model_call(self):
        limits=Limits(model_calls=1);run=self.create(limits)
        result=await self.execution(limits=limits).harness.execute(run.run_id,self.identity)
        self.assertEqual(result.outcome_code,'BUDGET_EXCEEDED');self.assertIsNone(result.answer)

    async def test_write_cannot_bypass_tool_registry(self):
        run=self.create();execution=self.execution()
        async def malicious(*args):return ToolCall('jenkins_request_tests',{'job':'production'})
        execution.harness.llm.decide=malicious
        result=await execution.harness.execute(run.run_id,self.identity)
        self.assertEqual(result.outcome_code,'UNKNOWN_TOOL');self.assertEqual(result.tool_calls,0)

    async def test_worker_pause_and_resume_keeps_ledger_and_question(self):
        run=self.runs.enqueue(Run(self.identity.workspace_id,self.identity.actor_id,execution_profile=PROFILE,
            read_request={'query':'¿Quién coordina Atlas?'},limits_snapshot=asdict(LIMITS)),self.identity,pause_at='jira')
        worker=Worker(self.runs,identity=self.identity,identity_resolver=self.knowledge.identity,
            composer=lambda profile,identity,store,**kwargs:self.execution(store,**kwargs))
        self.assertEqual((await worker.run_once())['event'],'paused')
        self.runs.requeue_job(run.run_id,self.identity)
        self.assertEqual((await worker.run_once())['status'],'COMPLETED')
        result=self.runs.load(run.run_id,self.identity)
        self.assertEqual((result.model_calls,result.tool_calls),(2,1))


class ReaderAPITests(unittest.TestCase):
    cleanup=api_tests.KnowledgeAPITests.cleanup
    setUp=api_tests.KnowledgeAPITests.setUp
    headers=api_tests.KnowledgeAPITests.headers

    def test_admission_is_authenticated_durable_and_has_no_inline_inference(self):
        with patch('harness.llm_reader.OllamaReader.decide',side_effect=AssertionError('inline inference')):
            response=self.client.post('/api/knowledge/reader/runs',headers=self.headers(),json={'query':'Atlas','pause_after_search':True})
        self.assertEqual(response.status_code,202,response.text)
        data=response.json();self.assertEqual(data['model_calls'],0);self.assertEqual(data['read_request'],{'query':'Atlas'})
        path='/api/knowledge/reader/runs/'+data['run_id']
        self.assertEqual(self.client.get(path,headers=self.headers(self.beta)).status_code,404)
        self.assertEqual(self.client.get('/api/runs/'+data['run_id'],headers=self.headers()).status_code,404)
        self.assertEqual(self.client.get('/api/knowledge/agent/runs/'+data['run_id'],headers=self.headers()).status_code,404)
        listing=self.client.get('/api/console/runs',headers=self.headers()).json()['runs']
        self.assertEqual(listing[0]['kind'],'reader');self.assertNotIn('read_request',listing[0])

    def test_no_client_model_identity_or_empty_question_and_revoked_session(self):
        for body in ({'query':' '},{'query':'Atlas','model':'arbitrary'},{'query':'Atlas','workspace_id':'other'}):
            self.assertIn(self.client.post('/api/knowledge/reader/runs',headers=self.headers(),json=body).status_code,(422,))
        self.directory.revoke(self.credential['session_id'])
        self.assertEqual(self.client.post('/api/knowledge/reader/runs',headers=self.headers(),json={'query':'Atlas'}).status_code,401)
