import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from harness.contracts import ContractError,Run,FinalAnswer,ToolResult
from harness.llm_reader import PROFILE,MODEL,MODEL_DIGEST,ABSTENTION,OllamaReader,finish
from harness.rag_agent import source
from harness.storage import encode,decode,check_update


def sample():
    run=Run('workspace-alpha','reader-demo',execution_profile=PROFILE,read_request={'query':'¿Quién coordina Atlas?'})
    citation='['+'a'*32+':11111111-1111-4111-8111-111111111111:0]'
    data={'query':'Atlas','limit':3,'index_id':'a'*32,'matches':[{'citation':citation,'filename':'atlas.md',
         'text':'Marta coordina Atlas.','distance':.1,'trust':'untrusted_document'}]}
    run.results=[ToolResult('call',data,source(data),tool_name='knowledge_search')]
    return run,citation


class ReaderContractTests(unittest.TestCase):
    def test_answer_requires_real_citation_and_exact_quote(self):
        run,citation=sample()
        data={'status':'answered','answer':'Marta','evidence':[{'citation':citation,'quote':'Marta coordina Atlas.'}]}
        self.assertIn('Marta',finish(run,FinalAnswer(json.dumps(data)))[0].text)
        for evidence in ({'citation':'forged','quote':'Marta'}, {'citation':citation,'quote':'invented'}):
            data['evidence']=[evidence]
            with self.assertRaisesRegex(ContractError,'INVALID_MODEL_OUTPUT'):finish(run,FinalAnswer(json.dumps(data)))

    def test_abstention_is_canonical_and_cannot_hide_an_answer(self):
        run,_=sample();data={'status':'abstained','answer':'','evidence':[]}
        self.assertEqual(finish(run,FinalAnswer(json.dumps(data)))[0].text,ABSTENTION)
        data['answer']='Invented'
        with self.assertRaises(ContractError):finish(run,FinalAnswer(json.dumps(data)))

    def test_empty_evidence_cannot_support_answer(self):
        run,citation=sample();run.results[0].data['matches']=[]
        data={'status':'answered','answer':'Marta','evidence':[{'citation':citation,'quote':'Marta'}]}
        with self.assertRaises(ContractError):finish(run,FinalAnswer(json.dumps(data)))

    def test_question_roundtrip_and_immutability_and_old_payload(self):
        from harness.composition import demo_identity
        run,_=sample();self.assertEqual(decode(encode(run)).read_request,run.read_request)
        with self.assertRaisesRegex(ContractError,'INVALID_CHECKPOINT'):
            check_update(run,replace(run,read_request={'query':'changed'}),demo_identity())
        payload=json.loads(encode(Run('workspace-alpha','reader-demo')));payload.pop('read_request')
        self.assertIsNone(decode(json.dumps(payload)).read_request)


class ReaderTransportTests(unittest.IsolatedAsyncioTestCase):
    def client(self,response):
        return NS(list=AsyncMock(return_value=NS(models=[NS(model=MODEL,digest=MODEL_DIGEST)])),
                  chat=AsyncMock(return_value=response),_client=NS(aclose=AsyncMock()))

    async def test_extra_tool_calls_or_writes_are_rejected(self):
        run,_=sample();run.results=[]
        for names in (['jenkins_request_tests'],['knowledge_search','knowledge_search']):
            response=NS(model=MODEL,done=True,done_reason='stop',message=NS(tool_calls=None,content=json.dumps({'name':names[0],'arguments':{'query':'Atlas','limit':3},**({'extra_call':names[1]} if len(names)>1 else {})})))
            client=self.client(response)
            with patch('harness.llm_reader.AsyncClient',return_value=client):
                with self.assertRaisesRegex(ContractError,'INVALID_MODEL_OUTPUT'):await OllamaReader().decide(run,('knowledge_search',))
            client._client.aclose.assert_awaited_once()

    async def test_model_pin_blocks_inference_and_cancellation_propagates(self):
        run,_=sample();run.results=[];client=self.client(None)
        client.list.return_value.models[0].digest='changed'
        with patch('harness.llm_reader.AsyncClient',return_value=client):
            with self.assertRaisesRegex(ContractError,'LLM_MODEL_CHANGED'):await OllamaReader().decide(run,('knowledge_search',))
        client.chat.assert_not_awaited()
        client=self.client(None);started=asyncio.Event()
        async def slow(**kwargs):started.set();await asyncio.sleep(30)
        client.chat.side_effect=slow
        with patch('harness.llm_reader.AsyncClient',return_value=client):
            task=asyncio.create_task(OllamaReader().decide(run,('knowledge_search',)))
            await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(client.list.await_count,1)
        client._client.aclose.assert_awaited_once()

    async def test_fixed_transport_and_single_real_tool_decision(self):
        run,_=sample();run.results=[]
        response=NS(model=MODEL,done=True,done_reason='stop',message=NS(tool_calls=None,content=json.dumps({'name':'knowledge_search','arguments':{'query':'Atlas','limit':3}})))
        client=self.client(response)
        with patch('harness.llm_reader.AsyncClient',return_value=client) as factory:
            decision=await OllamaReader().decide(run,('knowledge_search',))
        self.assertEqual(decision.name,'knowledge_search');self.assertEqual(client.list.await_count,2)
        self.assertEqual(factory.call_args.kwargs['host'],'http://127.0.0.1:11434')
        self.assertFalse(factory.call_args.kwargs['trust_env']);self.assertFalse(factory.call_args.kwargs['follow_redirects'])
        self.assertFalse(client.chat.call_args.kwargs['think'])


class ReaderEvaluationTests(unittest.TestCase):
    def test_irrelevant_citation_and_false_abstention_fail_positive(self):
        from scripts.evaluate_reader import grade_case
        run,citation=sample();run.status='COMPLETED';run.answer='Marta'
        run.assessment={'answer_status':'ANSWERED','evidence':[{'citation':citation,'quote':'Marta'}]}
        case={'id':'positive','expected_status':'ANSWERED','relevant':['release'],'expected_any':['marta']}
        identifier=citation.split(':')[1]
        self.assertTrue(grade_case(case,run,{identifier:'release'})['passed'])
        self.assertFalse(grade_case(case,run,{identifier:'unrelated'})['passed'])
        run.assessment={'answer_status':'ABSTAINED'};run.answer=ABSTENTION
        self.assertFalse(grade_case(case,run,{identifier:'release'})['passed'])

    def test_failure_is_not_an_abstention_and_invented_answer_fails_negative(self):
        from scripts.evaluate_reader import grade_case
        run,_=sample();case={'id':'negative','expected_status':'ABSTAINED','relevant':[],'expected_any':[]}
        run.status='FAILED';run.assessment={'answer_status':'ABSTAINED'};run.answer=ABSTENTION
        self.assertFalse(grade_case(case,run,{})['passed'])
        run.status='COMPLETED';run.assessment={'answer_status':'ANSWERED'};run.answer='invented@example.test'
        self.assertFalse(grade_case(case,run,{})['passed'])
        run.assessment={'answer_status':'ABSTAINED'};run.answer=ABSTENTION
        self.assertTrue(grade_case(case,run,{})['passed'])
