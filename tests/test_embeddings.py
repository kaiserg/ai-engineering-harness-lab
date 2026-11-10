import asyncio
import json
import math
import unittest
from unittest.mock import patch
import httpx
from harness.contracts import ContractError
from harness.embeddings import MODEL,MODEL_DIGEST,DIMENSION,INSTRUCTION,OllamaEmbedding,provider_for
from scripts.evaluate_retrieval import grade


class EmbeddingTests(unittest.TestCase):
    def vector(self):return [1.0]+[0.0]*(DIMENSION-1)
    def test_digest_change_is_rejected_before_inference(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200,json={'models':[{'name':MODEL,'digest':'changed'}]})
        client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch('harness.embeddings.httpx.Client',return_value=client):
            with self.assertRaisesRegex(ContractError,'EMBEDDING_MODEL_CHANGED'):OllamaEmbedding().query('test')
        self.assertEqual(calls,['/api/tags'])

    def test_prefix_truncation_scope_and_pin_are_explicit(self):
        requests=[]
        def handler(request):
            requests.append(request)
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':MODEL,'digest':MODEL_DIGEST}]})
            body=json.loads(request.content)
            self.assertEqual(body['input'],[INSTRUCTION+'pregunta']);self.assertFalse(body['truncate'])
            self.assertEqual(request.url.host,'127.0.0.1');self.assertNotIn('authorization',request.headers)
            return httpx.Response(200,json={'model':MODEL,'embeddings':[self.vector()]})
        client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch('harness.embeddings.httpx.Client',return_value=client):
            self.assertEqual(len(OllamaEmbedding().query('pregunta')),DIMENSION)
        self.assertEqual([r.url.path for r in requests],['/api/tags','/api/embed','/api/tags'])

    def test_malformed_embeddings_are_not_accepted(self):
        for vector in ([1.0], [0.0]*DIMENSION, [math.nan]+[0.0]*(DIMENSION-1)):
            with self.assertRaisesRegex(ContractError,'INVALID_EMBEDDING'):
                OllamaEmbedding.vectors({'model':MODEL,'embeddings':[vector]},1)
        with self.assertRaises(ContractError):provider_for('arbitrary:remote-provider')

    def test_grade_measures_rank_and_recall_without_claiming_answer_quality(self):
        self.assertEqual(grade(['a','b','c'],['b']),{'top1':0,'recall_at_3':1.0,'reciprocal_rank_at_3':.5})
        self.assertEqual(grade(['c'],['a'])['recall_at_3'],0)
        self.assertEqual(grade(['a','b'],['a','b'])['recall_at_3'],1)

    def test_index_budget_is_checked_after_embedding_before_chroma_write(self):
        from harness.chroma_vectors import ChromaVectors
        vectors=ChromaVectors()
        chunks=[{'chunk_id':'one','document_id':'doc','content':'text'}]
        with patch.object(vectors,'request') as request,patch.object(vectors,'collection',return_value='id'),patch('harness.chroma_vectors.time.monotonic',side_effect=[0,1,31]):
            with self.assertRaisesRegex(ContractError,'INDEX_DEADLINE'):vectors.build('harness_rag_'+'a'*32,chunks,lambda text:[1.0])
        self.assertEqual(request.call_count,1)  # Solo creó candidato; no escribió vectores fuera del presupuesto.


class AsyncEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_stops_embedding_request_and_postcheck(self):
        calls=[];started=asyncio.Event();cancelled=asyncio.Event()
        async def handler(request):
            calls.append(request.url.path)
            if request.url.path=='/api/tags':return httpx.Response(200,json={'models':[{'name':MODEL,'digest':MODEL_DIGEST}]})
            started.set()
            try:await asyncio.sleep(30)
            except asyncio.CancelledError:cancelled.set();raise
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('harness.embeddings.httpx.AsyncClient',return_value=client):
            task=asyncio.create_task(OllamaEmbedding().aquery('test'));await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(cancelled.is_set());self.assertEqual(calls,['/api/tags','/api/embed'])
